# Author: Toshio Kuratomi <tkuratom@redhat.com>
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or
# https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2020, Ansible Project
"""Functions to work with Galaxy."""

from __future__ import annotations

import os.path
import shutil
import typing as t
from urllib.parse import urljoin

import aiofiles
import semantic_version as semver

from . import app_context
from .utils.hashing import verify_hash
from .utils.http import retry_get

# The type checker can handle finding aiohttp.client but flake8 cannot :-(
if t.TYPE_CHECKING:
    import aiohttp.client  # pylint:disable=unused-import


#: URL to galaxy. (Get the default from the context default)
_GALAXY_SERVER_URL = str(app_context.AppContext().galaxy_url)


class NoSuchCollection(Exception):
    """Collection name does not map to a collection on Galaxy."""


class NoSuchVersion(Exception):
    """Version does not match with any versions of a collection on Galaxy."""


class DownloadFailure(Exception):
    """Failure downloading a collection from Galaxy."""


class DownloadResults(t.NamedTuple):
    """Results of downloading a collection."""

    #: :obj:`semantic_version.Version` of the exact version of the collection that was downloaded.
    version: semver.Version
    #: Location on the filesystem of the downloaded collection.
    download_path: str


class GalaxyClient:
    """Class for querying the Galaxy REST API."""

    def __init__(self, aio_session: aiohttp.client.ClientSession,
                 galaxy_server: str = _GALAXY_SERVER_URL) -> None:
        """
        Create a GalaxyClient object to query the Galaxy Server.

        :arg aio_session: :obj:`aiohttp.ClientSession` with which to perform all
            requests to galaxy.
        :kwarg galaxy_server: URL to the galaxy server.
        """
        self.galaxy_server = galaxy_server
        self.aio_session = aio_session
        self.params = {'format': 'json'}

    async def _get_galaxy_versions(self, versions_url: str) -> list[str]:
        """
        Retrieve the complete list of versions for a collection from a galaxy endpoint.

        This internal function retrieves versions for collections from a Galaxy endpoint.  If the
        information is paged, it continues to retrieve linked pages until all of the information has
        been returned.

        :arg version_url: url to the page to retrieve.
        :returns: List of the all the versions of the collection.
        """
        params = self.params.copy()
        params['page_size'] = '100'
        async with retry_get(self.aio_session, versions_url, params=params,
                             acceptable_error_codes=[404]) as response:
            if response.status == 404:
                raise NoSuchCollection(f'No collection found at: {versions_url}')
            collection_info = await response.json()

        versions = []
        for version_record in collection_info['results']:
            versions.append(version_record['version'])

        if collection_info['next']:
            versions.extend(await self._get_galaxy_versions(collection_info['next']))

        return versions

    async def get_versions(self, collection: str) -> list[str]:
        """
        Retrieve all versions of a collection on Galaxy.

        :arg collection: Name of the collection to get version info for.
        :returns: List of all the versions of this collection on galaxy.
        """
        collection = collection.replace('.', '/')
        galaxy_url = urljoin(self.galaxy_server, f'api/v2/collections/{collection}/versions/')
        retval = await self._get_galaxy_versions(galaxy_url)
        return retval

    async def get_info(self, collection: str) -> dict[str, t.Any]:
        """
        Retrieve information about the collection on Galaxy.

        :arg collection: Namespace.collection to retrieve information about.
        :returns: Dictionary of information about the collection.

        Please see the Galaxy REST API documentation for information on the structure of the
        returned data.

        .. seealso::
            An example return value from the
            `Galaxy REST API <https://galaxy.ansible.com/api/v2/collections/community/general/>`_
        """
        collection = collection.replace('.', '/')
        galaxy_url = urljoin(self.galaxy_server, f'api/v2/collections/{collection}/')

        async with retry_get(self.aio_session, galaxy_url, params=self.params,
                             acceptable_error_codes=[404]) as response:
            if response.status == 404:
                raise NoSuchCollection(f'No collection found at: {galaxy_url}')
            collection_info = await response.json()

        return collection_info

    async def get_release_info(self, collection: str,
                               version: str | semver.Version) -> dict[str, t.Any]:
        """
        Retrive information about a specific version of a collection.

        :arg collection: Namespace.collection string naming the collection.
        :arg version: Version of the collection.
        :returns: Dictionary of information about the release.

        Please see the Galaxy REST API documentation for information on the structure of the
        returned data.

        .. seealso::
            An example return value from the
            `Galaxy REST API
            <https://galaxy.ansible.com/api/v2/collections/community/general/versions/0.1.1>`_
        """
        collection = collection.replace('.', '/')
        galaxy_url = urljoin(self.galaxy_server,
                             f'api/v2/collections/{collection}/versions/{version}/')

        async with retry_get(self.aio_session, galaxy_url, params=self.params,
                             acceptable_error_codes=[404]) as response:
            if response.status == 404:
                raise NoSuchCollection(f'No collection found at: {galaxy_url}')
            collection_info = await response.json()

        return collection_info

    async def get_latest_matching_version(self, collection: str,
                                          version_spec: str,
                                          pre: bool = False) -> semver.Version:
        """
        Get the latest version of a collection that matches a specification.

        :arg collection: Namespace.collection identifying a collection.
        :arg version_spec: String specifying the allowable versions.
        :kwarg pre: If True, allow prereleases (versions which have the form X.Y.Z.SOMETHING).
            This is **not** for excluding 0.Y.Z versions.  non-pre-releases are still
            preferred over pre-releases (for instance, with version_spec='>2.0.0-a1,<3.0.0'
            and pre=True, if the available versions are 2.0.0-a1 and 2.0.0-a2, then 2.0.0-a2
            will be returned.  If the available versions are 2.0.0 and 2.1.0-b2, 2.0.0 will be
            returned since non-pre-releases are preferred.  The default is False
        :returns: :obj:`semantic_version.Version` of the latest collection version that satisfied
            the specification.

        .. seealso:: For the format of the version_spec, see the documentation
            of :obj:`semantic_version.SimpleSpec`

        .. versionchanged:: 0.37.0
            Giving True to the ``pre`` parameter now means that prereleases will be
            *allowed* but stable releases will still be *preferred*.  Previously, the
            latest release, whether stable or prerelease was returned when pre was True.
        """
        versions = await self.get_versions(collection)
        sem_versions = [semver.Version(v) for v in versions]
        sem_versions.sort(reverse=True)

        spec = semver.SimpleSpec(version_spec)
        prereleases = []
        for version in (v for v in sem_versions if v in spec):
            # If this is a pre-release, first check if there's a non-pre-release that
            # will satisfy the version_spec.
            if version.prerelease:
                prereleases.append(version)
                continue
            return version

        # We did not find a stable version that satisies the version_spec.  If we
        # allow prereleases, return the latest of those here.
        if pre and prereleases:
            return prereleases[0]

        # No matching versions were found
        raise NoSuchVersion(f'{version_spec} did not match with any version of {collection}.')


class CollectionDownloader(GalaxyClient):
    """Manage downloading collections from Galaxy."""

    def __init__(self, aio_session: aiohttp.client.ClientSession,
                 download_dir: str,
                 galaxy_server: str = _GALAXY_SERVER_URL,
                 collection_cache: str | None = None) -> None:
        """
        Create an object to download collections from galaxy.

        :arg aio_session: :obj:`aiohttp.ClientSession` with which to perform all
            requests to galaxy.
        :arg download_dir: Directory to download into.
        :kwarg galaxy_server: URL to the galaxy server.
        :kwarg collection_cache: If given, a path to a directory containing collection tarballs.
            These tarballs will be used instead of downloading new tarballs provided that the
            versions match the criteria (latest compatible version known to galaxy).
        """
        super().__init__(aio_session, galaxy_server)
        self.download_dir = download_dir
        self.collection_cache: t.Final[str | None] = collection_cache

    async def download(self, collection: str, version: str | semver.Version, ) -> str:
        """
        Download a collection.

        Downloads the collection at the specified version.

        :arg collection: Namespace.collection identifying the collection.
        :arg version: Version of the collection to download.
        :returns: The full path to the downloaded collection.
        """
        collection = collection.replace('.', '/')
        release_info = await self.get_release_info(collection, version)
        release_url = release_info['download_url']

        download_filename = os.path.join(self.download_dir, release_info['artifact']['filename'])
        sha256sum = release_info['artifact']['sha256']

        if self.collection_cache:
            if release_info['artifact']['filename'] in os.listdir(self.collection_cache):
                cached_copy = os.path.join(self.collection_cache,
                                           release_info['artifact']['filename'])
                if await verify_hash(cached_copy, sha256sum):
                    shutil.copyfile(cached_copy, download_filename)
                return download_filename

        async with retry_get(self.aio_session, release_url,
                             acceptable_error_codes=[404]) as response:
            if response.status == 404:
                raise NoSuchCollection(f'No collection found at: {release_url}')

            async with aiofiles.open(download_filename, 'wb') as f:
                lib_ctx = app_context.lib_ctx.get()
                while chunk := await response.content.read(lib_ctx.chunksize):
                    await f.write(chunk)

        # Verify the download
        if not await verify_hash(download_filename, sha256sum):
            raise DownloadFailure(f'{release_url} failed to download correctly.'
                                  f' Expected checksum: {sha256sum}')

        # Copy downloaded collection into cache
        if self.collection_cache:
            cached_copy = os.path.join(self.collection_cache,
                                       release_info['artifact']['filename'])
            shutil.copyfile(download_filename, cached_copy)

        return download_filename

    async def download_latest_matching(self, collection: str,
                                       version_spec: str) -> DownloadResults:
        """
        Download the latest version of a collection that matches a specification.

        :arg collection: Namespace.collection identifying a collection.
        :arg version_spec: String specifying the allowable versions.
        :returns: :obj:`DownloadResults` with version and download path for the collection we
            downloaded.

        .. seealso:: For the format of the version_spec, see the documentation
            of :obj:`semantic_version.SimpleSpec`
        """
        version = await self.get_latest_matching_version(collection, version_spec)
        download_path = await self.download(collection, version)
        return DownloadResults(version=version, download_path=download_path)
