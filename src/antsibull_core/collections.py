# Author: Toshio Kuratomi <tkuratom@redhat.com>
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or
# https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2020, Ansible Project
"""Functions to deal with collections on the local system"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import sh

from . import app_context


class CollectionFormatError(Exception):
    pass


async def install_together(collection_tarballs: list[str],
                           ansible_collections_dir: str) -> None:
    loop = asyncio.get_running_loop()
    lib_ctx = app_context.lib_ctx.get()
    executor = ThreadPoolExecutor(max_workers=lib_ctx.thread_max)

    installers = []
    for pathname in collection_tarballs:
        namespace, collection, _dummy = os.path.basename(pathname).split('-', 2)
        collection_dir = os.path.join(ansible_collections_dir, namespace, collection)
        # Note: mkdir -p equivalent is okay because we created package_dir ourselves as a directory
        # that only we can access
        os.makedirs(collection_dir, mode=0o700, exist_ok=False)

        # If the choice of install tools for galaxy is ever settled upon, we can switch from tar to
        # using that
        # sh dynamically creates functions which map to executables
        # pyre-ignore[16] pylint:disable-next=no-member
        installers.append(loop.run_in_executor(executor, sh.tar, '-xf', pathname, '-C',
                                               collection_dir))

    await asyncio.gather(*installers)


async def install_separately(collection_tarballs: list[str], collection_dir: str) -> list[str]:
    installers = []
    collection_dirs: list[str] = []

    if not collection_tarballs:
        return collection_dirs

    loop = asyncio.get_running_loop()
    lib_ctx = app_context.lib_ctx.get()
    executor = ThreadPoolExecutor(max_workers=lib_ctx.thread_max)

    for pathname in collection_tarballs:
        filename = os.path.basename(pathname)
        namespace, collection, version_ext = filename.split('-', 2)
        version = None
        for ext in ('.tar.gz',):
            # Note: If galaxy allows other archive formats, add their extensions here
            ext_start = version_ext.find(ext)
            if ext_start != -1:
                version = version_ext[:ext_start]
                break
        else:
            raise CollectionFormatError('Collection filename was in an unexpected'
                                        f' format: {filename}')

        package_dir = os.path.join(collection_dir, f'ansible-collections-{namespace}.'
                                   f'{collection}-{version}')
        os.mkdir(package_dir, mode=0o700)
        collection_dirs.append(package_dir)

        collection_dir = os.path.join(package_dir, 'ansible_collections', namespace, collection)
        # Note: this is okay because we created package_dir ourselves as a directory
        # that only we can access
        os.makedirs(collection_dir, mode=0o700, exist_ok=False)

        # If the choice of install tools for galaxy is ever settled upon, we can switch from tar to
        # using that
        # sh dynamically creates functions which map to executables
        # pyre-ignore[16] pylint:disable-next=no-member
        installers.append(loop.run_in_executor(executor, sh.tar, '-xf', pathname, '-C',
                                               collection_dir))

    await asyncio.gather(*installers)

    return collection_dirs
