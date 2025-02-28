# Author: Toshio Kuratomi <tkuratom@redhat.com>
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or
# https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2020, Ansible Project

"""Functions for working with the ansible-core package."""

from __future__ import annotations

import ast
import asyncio
import os
import re
import tempfile
import typing as t
from functools import lru_cache, partial
from urllib.parse import urljoin

import aiofiles
import sh
from packaging.version import Version as PypiVer

from . import app_context
from .logging import log
from .utils.http import retry_get

if t.TYPE_CHECKING:
    import aiohttp.client  # pylint:disable=unused-import


mlog = log.fields(mod=__name__)

#: URL to checkout ansible-core from.
_ANSIBLE_CORE_URL = str(app_context.AppContext().ansible_base_url)
#: URL to pypi.
_PYPI_SERVER_URL = str(app_context.AppContext().pypi_url)


class UnknownVersion(Exception):
    """Raised when a requested version does not exist."""


class CannotBuild(Exception):
    """Raised when we can't figure out how to build a package."""


class AnsibleCorePyPiClient:
    """Class to retrieve information about ansible-core from Pypi."""

    def __init__(self, aio_session: 'aiohttp.client.ClientSession',
                 pypi_server_url: str = _PYPI_SERVER_URL) -> None:
        """
        Initialize the AnsibleCorePypiClient class.

        :arg aio_session: :obj:`aiohttp.client.ClientSession` to make requests to pypi from.
        :kwarg pypi_server_url: URL to the pypi server to use.
        """
        self.aio_session = aio_session
        self.pypi_server_url = pypi_server_url

    async def _get_json(self, query_url: str) -> dict[str, t.Any]:
        """
        JSON data from a url with retries and return the data as python data structures.
        """
        async with retry_get(self.aio_session, query_url) as response:
            pkg_info = await response.json()
        return pkg_info

    @lru_cache(None)
    async def get_release_info(self) -> dict[str, t.Any]:
        """
        Retrieve information about releases of the ansible-core/ansible-base package from pypi.

        :returns: The dict which represents the release info keyed by version number.
            To examine the data structure, use::

                curl https://pypi.org/pypi/ansible-core/json| python3 -m json.tool

        .. note:: Returns an aggregate of ansible-base and ansible-core releases.
        """
        # Retrieve the ansible-base and ansible-core package info from pypi
        tasks = []
        for package_name in ('ansible-core', 'ansible-base'):
            query_url = urljoin(self.pypi_server_url, f'pypi/{package_name}/json')
            tasks.append(asyncio.create_task(self._get_json(query_url)))

        # Note: gather maintains the order of results
        results = await asyncio.gather(*tasks)
        release_info = results[1]['releases']  # ansible-base information
        release_info.update(results[0]['releases'])  # ansible-core information

        return release_info

    async def get_versions(self) -> list[PypiVer]:
        """
        Get the versions of the ansible-core package on pypi.

        :returns: A list of :pypkg:obj:`packaging.versioning.Version`s
            for all the versions on pypi, including prereleases.
        """
        flog = mlog.fields(func='AnsibleCorePyPiClient.get_versions')
        flog.debug('Enter')

        release_info = await self.get_release_info()
        versions = [PypiVer(r) for r in release_info]
        versions.sort(reverse=True)
        flog.fields(versions=versions).info('sorted list of ansible-core versions')

        flog.debug('Leave')
        return versions

    async def get_latest_version(self) -> PypiVer:
        """
        Get the latest version of ansible-core uploaded to pypi.

        :return: A :pypkg:obj:`packaging.versioning.Version` object representing the latest version
            of the package on pypi.  This may be a pre-release.
        """
        versions = await self.get_versions()
        return versions[0]

    async def retrieve(self, ansible_core_version: str, download_dir: str) -> str:
        """
        Get the release from pypi.

        :arg ansible_core_version: Version of ansible-core to download.
        :arg download_dir: Directory to download the tarball to.
        :returns: The name of the downloaded tarball.
        """
        package_name = get_ansible_core_package_name(ansible_core_version)
        release_info = await self.get_release_info()

        pypi_url = tar_filename = ''
        for release in release_info[ansible_core_version]:
            if release['filename'].startswith(f'{package_name}-{ansible_core_version}.tar.'):
                tar_filename = release['filename']
                pypi_url = release['url']
                break
        else:  # for-else: http://bit.ly/1ElPkyg
            raise UnknownVersion(f'{package_name} {ansible_core_version} does not'
                                 ' exist on {self.pypi_server_url}')

        tar_filename = os.path.join(download_dir, tar_filename)
        async with retry_get(self.aio_session, pypi_url) as response:
            async with aiofiles.open(tar_filename, 'wb') as f:
                lib_ctx = app_context.lib_ctx.get()
                while chunk := await response.content.read(lib_ctx.chunksize):
                    await f.write(chunk)

        return tar_filename


def get_ansible_core_package_name(ansible_core_version: str | PypiVer) -> str:
    """
    Returns the name of the minimal ansible package.

    :arg ansible_core_version: The version of the minimal ansible package to retrieve the
        name for.
    :returns: 'ansible-core' when the version is 2.11 or higher. Otherwise 'ansible-base'.
    """
    if not isinstance(ansible_core_version, PypiVer):
        ansible_core_version = PypiVer(ansible_core_version)

    if ansible_core_version.major <= 2 and ansible_core_version.minor <= 10:
        return 'ansible-base'

    return 'ansible-core'


def _get_source_version(ansible_core_source: str) -> PypiVer:
    with open(os.path.join(ansible_core_source, 'lib', 'ansible', 'release.py'),
              'r', encoding='utf-8') as f:
        root = ast.parse(f.read())

    # Find the version of the source
    source_version = None
    # Iterate backwards in case __version__ is assigned to multiple times
    for node in reversed(root.body):
        if isinstance(node, ast.Assign):
            for name in node.targets:
                # These attributes are dynamic so pyre cannot check them
                if name.id == '__version__':  # type: ignore[attr-defined] # pyre-ignore[16]
                    source_version = node.value.s  # type: ignore[attr-defined] # pyre-ignore[16]
                    break

        if source_version:
            break

    if not source_version:
        raise ValueError('Version was not found')

    return PypiVer(source_version)


def _version_is_devel(version: PypiVer) -> bool:
    dev_version = re.compile('.*[.]dev[0-9]+$')
    return bool(dev_version.match(version.public))


def source_is_devel(ansible_core_source: str | None) -> bool:
    """
    :arg ansible_core_source: A path to an Ansible-core checkout or expanded sdist or None.
        This will be used instead of downloading an ansible-core package if the version matches
        with ``ansible_core_version``.
    :returns: True if the source looks like it is for the devel branch.
    """
    if ansible_core_source is None:
        return False

    try:
        source_version = _get_source_version(ansible_core_source)
    except Exception:  # pylint:disable=broad-except
        return False

    return _version_is_devel(source_version)


def source_is_correct_version(ansible_core_source: str | None,
                              ansible_core_version: PypiVer) -> bool:
    """
    :arg ansible_core_source: A path to an Ansible-core checkout or expanded sdist or None.
        This will be used instead of downloading an ansible-core package if the version matches
        with ``ansible_core_version``.
    :arg ansible_core_version: Version of ansible-core to retrieve.
    :returns: True if the source is for a compatible version at or newer than the requested version
    """
    if ansible_core_source is None:
        return False

    try:
        source_version = _get_source_version(ansible_core_source)
    except Exception:  # pylint:disable=broad-except
        return False

    # If the source is a compatible version of ansible-core and it is the same or more recent than
    # the requested version then allow this.
    if (source_version.major == ansible_core_version.major
            and source_version.minor == ansible_core_version.minor
            and source_version.micro >= ansible_core_version.micro):
        return True

    return False


@lru_cache(None)
async def checkout_from_git(download_dir: str, repo_url: str = _ANSIBLE_CORE_URL) -> str:
    """
    Checkout the ansible-core git repo.

    :arg download_dir: Directory to checkout into.
    :kwarg: repo_url: The url to the git repo.
    :return: The directory that ansible-core has been checked out to.
    """
    loop = asyncio.get_running_loop()
    ansible_core_dir = os.path.join(download_dir, 'ansible-core')
    await loop.run_in_executor(None, sh.git, 'clone', repo_url, ansible_core_dir)

    return ansible_core_dir


@lru_cache(None)
async def create_sdist(source_dir: str, dest_dir: str) -> str:
    """
    Create an sdist for the python package at a given path.

    Note that this is not able to create an sdist for any python package.  It has to have a setup.py
    sdist command.

    :arg source_dir: the directory that the python package source is in.
    :arg dest_dir: the directory that the sdist will be written to/
    :returns: path to the sdist.
    """
    loop = asyncio.get_running_loop()

    # Make sure setup.py exists
    setup_script = os.path.join(source_dir, 'setup.py')
    if not os.path.exists(setup_script):
        raise CannotBuild(f'{source_dir} does not include a setup.py script.  This script cannot'
                          ' build the package')

    # Make a subdir of dest_dir for returning the dist in
    dist_dir_prefix = os.path.join(os.path.basename(source_dir))
    dist_dir = tempfile.mkdtemp(prefix=dist_dir_prefix, dir=dest_dir)

    # execute python setup.py sdist --dist-dir dest_dir/
    # sh maps attributes to commands dynamically so ignore the linting errors there
    # pyre-ignore[16]
    python_cmd = partial(sh.python, _cwd=source_dir)  # pylint:disable=no-member
    try:
        await loop.run_in_executor(None, python_cmd, setup_script, 'sdist', '--dist-dir', dist_dir)
    except Exception as e:
        raise CannotBuild(f'Building {source_dir} failed: {e}')  # pylint:disable=raise-missing-from

    dist_files = [f for f in os.listdir(dist_dir) if f.endswith('tar.gz')]
    if len(dist_files) != 1:
        if not dist_files:
            raise CannotBuild(f'Building {source_dir} did not create a tar.gz')

        raise CannotBuild(f'Building {source_dir} created more than one tar.gz files which is not'
                          ' yet supported.')

    return os.path.join(dist_dir, dist_files[0])


async def get_ansible_core(aio_session: 'aiohttp.client.ClientSession',
                           ansible_core_version: str,
                           tmpdir: str,
                           ansible_core_source: str | None = None) -> str:
    """
    Create an ansible-core directory of the requested version.

    :arg aio_session: :obj:`aiohttp.client.ClientSession` to make http requests with.
    :arg ansible_core_version: Version of ansible-core to retrieve.  If it is the special string
        ``@devel``, then we will retrieve ansible-core from its git repository.  If it is the
        special string ``@latest``, then we will retrieve the latest version from pypi.
    :arg tmpdir: Temporary directory use as a scratch area for downloading to and the place that the
        ansible-core directory should be placed in.
    :kwarg ansible_core_source: If given, a path to an ansible-core checkout or expanded sdist.
        This will be used instead of downloading an ansible-core package if the version matches
        with ``ansible_core_version``.
    """
    if ansible_core_version == '@devel':
        # is the source usable?
        if source_is_devel(ansible_core_source):
            # source_is_devel() protects against this.  This assert is to inform the type checker
            assert ansible_core_source is not None
            source_location: str = ansible_core_source

        else:
            source_location = await checkout_from_git(tmpdir)

        # Create an sdist from the source that can be installed
        install_file = await create_sdist(source_location, tmpdir)
    else:
        pypi_client = AnsibleCorePyPiClient(aio_session)
        ansible_core_pypi_version: PypiVer
        if ansible_core_version == '@latest':
            ansible_core_pypi_version = await pypi_client.get_latest_version()
        else:
            ansible_core_pypi_version = PypiVer(ansible_core_version)

        # is the source the asked for version?
        if source_is_correct_version(ansible_core_source, ansible_core_pypi_version):
            assert ansible_core_source is not None
            # Create an sdist from the source that can be installed
            install_file = await create_sdist(ansible_core_source, tmpdir)
        else:
            install_file = await pypi_client.retrieve(ansible_core_pypi_version.public, tmpdir)

    return install_file
