import itertools
import json
import logging
import functools
import os
from copy import deepcopy
from os.path import basename
from typing import Dict, Set

from .genericpkgctx import InGenericPkgCtx
from .lddtree import lddtree
from .elfutils import (elf_file_filter, elf_find_versioned_symbols,
                       elf_references_PyFPE_jbuf,
                       elf_find_ucs2_symbols, elf_is_python_extension)
from .policy import (lddtree_external_references, versioned_symbols_policy,
                     get_policy_name, POLICY_PRIORITY_LOWEST,
                     POLICY_PRIORITY_HIGHEST, load_policies)

try:
    from collections.abc import defaultdict, Mapping, namedtuple
except ImportError:
    # Pre-Python 3.7 compatibility
    from collections import defaultdict, Mapping, namedtuple


log = logging.getLogger(__name__)
WheelAbIInfo = namedtuple('WheelAbIInfo',
                          ['overall_tag', 'external_refs', 'ref_tag',
                           'versioned_symbols', 'sym_tag', 'ucs_tag',
                           'pyfpe_tag'])


@functools.lru_cache()
def get_wheel_elfdata(wheel_fn: str):
    full_elftree = {}
    nonpy_elftree = {}
    full_external_refs = {}
    versioned_symbols = defaultdict(lambda: set())  # type: Dict[str, Set[str]]
    uses_ucs2_symbols = False
    uses_PyFPE_jbuf = False

    with InGenericPkgCtx(wheel_fn) as ctx:
        for fn, elf in elf_file_filter(ctx.iter_files()):
            is_py_ext, py_ver = elf_is_python_extension(fn, elf)

            # Check for invalid binary wheel format: no shared library should
            # be found in purelib
            so_path_split = fn.split(os.sep)
            if 'purelib' in so_path_split:
                raise RuntimeError(
                    ('Invalid binary wheel, found shared library "%s" in '
                     'purelib folder.\n'
                     'The wheel has to be platlib compliant in order to be '
                     'repaired by auditwheel.') %
                    so_path_split[-1])

            log.debug('processing: %s', fn)
            elftree = lddtree(fn)

            for key, value in elf_find_versioned_symbols(elf):
                log.debug('key %s, value %s', key, value)
                versioned_symbols[key].add(value)

            # If the ELF is a Python extension, we definitely need to include
            # its external dependencies.
            if is_py_ext:
                full_elftree[fn] = elftree
                uses_PyFPE_jbuf |= elf_references_PyFPE_jbuf(elf)
                if py_ver == 2:
                    uses_ucs2_symbols |= any(
                        True for _ in elf_find_ucs2_symbols(elf))
                full_external_refs[fn] = lddtree_external_references(elftree,
                                                                     ctx.path)
            else:
                # If the ELF is not a Python extension, it might be included in
                # the wheel already because auditwheel repair vendored it, so
                # we will check whether we should include its internal
                # references later.
                nonpy_elftree[fn] = elftree

        # Get a list of all external libraries needed by ELFs in the wheel.
        needed_libs = {
            lib
            for elf in itertools.chain(full_elftree.values(),
                                       nonpy_elftree.values())
            for lib in elf['needed']
        }

        for fn in nonpy_elftree.keys():
            # If a non-pyextension ELF file is not needed by something else
            # inside the wheel, then it was not checked by the logic above and
            # we should walk its elftree.
            if basename(fn) not in needed_libs:
                full_elftree[fn] = nonpy_elftree[fn]
                full_external_refs[fn] = lddtree_external_references(
                    nonpy_elftree[fn], ctx.path)

    log.debug(json.dumps(full_elftree, indent=4))

    return (full_elftree, full_external_refs, versioned_symbols,
            uses_ucs2_symbols, uses_PyFPE_jbuf)


def get_external_libs(external_refs):
    """Get external library dependencies for all policies excluding the default
    linux policy
    :param external_refs: external references for all policies
    :return: {realpath: soname} e.g.
    {'/path/to/external_ref.so.1.2.3': 'external_ref.so.1'}
    """
    result = {}
    for policy in external_refs.values():
        # linux tag (priority 0) has no white-list, do not analyze it
        if policy['priority'] == 0:
            continue
        # go through all libs, retrieving their soname and realpath
        for libname, realpath in policy['libs'].items():
            if realpath and realpath not in result.keys():
                result[realpath] = libname
    return result


def get_versioned_symbols(libs):
    """Get versioned symbols used in libraries
    :param libs: {realpath: soname} dict to search for versioned symbols e.g.
    {'/path/to/external_ref.so.1.2.3': 'external_ref.so.1'}
    :return: {soname: {depname: set([symbol_version])}} e.g.
    {'external_ref.so.1': {'libc.so.6', set(['GLIBC_2.5','GLIBC_2.12'])}}
    """
    result = {}
    for path, elf in elf_file_filter(libs.keys()):
        # {depname: set(symbol_version)}, e.g.
        # {'libc.so.6', set(['GLIBC_2.5','GLIBC_2.12'])}
        elf_versioned_symbols = defaultdict(lambda: set())
        for key, value in elf_find_versioned_symbols(elf):
            log.debug('path %s, key %s, value %s', path, key, value)
            elf_versioned_symbols[key].add(value)
        result[libs[path]] = elf_versioned_symbols
    return result


def get_symbol_policies(versioned_symbols, external_versioned_symbols,
                        external_refs):
    """Get symbol policies
    Since white-list is different per policy, this function inspects
    versioned_symbol per policy when including external refs
    :param versioned_symbols: versioned symbols for the current wheel
    :param external_versioned_symbols: versioned symbols for external libs
    :param external_refs: external references for all policies
    :return: list of tuples of the form (policy_priority, versioned_symbols),
    e.g. [(100, {'libc.so.6', set(['GLIBC_2.5'])})]
    """
    result = []
    for policy in external_refs.values():
        # skip the linux policy
        if policy['priority'] == 0:
            continue
        policy_symbols = deepcopy(versioned_symbols)
        for soname in policy['libs'].keys():
            if soname not in external_versioned_symbols:
                continue
            ext_symbols = external_versioned_symbols[soname]
            for k in iter(ext_symbols):
                policy_symbols[k].update(ext_symbols[k])
        result.append(
            (versioned_symbols_policy(policy_symbols), policy_symbols))
    return result


def analyze_wheel_abi(wheel_fn: str):
    external_refs = {
        p['name']: {'libs': {},
                    'priority': p['priority']}
        for p in load_policies()
    }

    (elftree_by_fn, external_refs_by_fn, versioned_symbols, has_ucs2,
     uses_PyFPE_jbuf) = get_wheel_elfdata(wheel_fn)

    for fn in elftree_by_fn.keys():
        update(external_refs, external_refs_by_fn[fn])

    log.debug('external reference info')
    log.debug(json.dumps(external_refs, indent=4))

    external_libs = get_external_libs(external_refs)
    external_versioned_symbols = get_versioned_symbols(external_libs)
    symbol_policies = get_symbol_policies(versioned_symbols,
                                          external_versioned_symbols,
                                          external_refs)
    symbol_policy = versioned_symbols_policy(versioned_symbols)

    # let's keep the highest priority policy and
    # corresponding versioned_symbols
    symbol_policy, versioned_symbols = max(
        symbol_policies,
        key=lambda x: x[0],
        default=(symbol_policy, versioned_symbols)
    )

    ref_policy = max(
        (e['priority'] for e in external_refs.values() if len(e['libs']) == 0),
        default=POLICY_PRIORITY_LOWEST)

    if has_ucs2:
        ucs_policy = POLICY_PRIORITY_LOWEST
    else:
        ucs_policy = POLICY_PRIORITY_HIGHEST

    if uses_PyFPE_jbuf:
        pyfpe_policy = POLICY_PRIORITY_LOWEST
    else:
        pyfpe_policy = POLICY_PRIORITY_HIGHEST

    ref_tag = get_policy_name(ref_policy)
    sym_tag = get_policy_name(symbol_policy)
    ucs_tag = get_policy_name(ucs_policy)
    pyfpe_tag = get_policy_name(pyfpe_policy)
    overall_tag = get_policy_name(min(symbol_policy, ref_policy, ucs_policy,
                                      pyfpe_policy))

    return WheelAbIInfo(overall_tag, external_refs, ref_tag, versioned_symbols,
                        sym_tag, ucs_tag, pyfpe_tag)


def update(d, u):
    for k, v in u.items():
        if isinstance(v, Mapping):
            r = update(d.get(k, {}), v)
            d[k] = r
        elif isinstance(v, (str, int, float, type(None))):
            d[k] = u[k]
        else:
            raise RuntimeError('!', d, k)
    return d
