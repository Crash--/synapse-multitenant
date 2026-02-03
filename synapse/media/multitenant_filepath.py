#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

"""
Multi-tenant media file paths for Synapse.

This module provides tenant-aware media file paths that isolate each tenant's
media files in separate directories. The path structure is:

    <base_path>/<tenant_server_name>/local_content/...
    <base_path>/<tenant_server_name>/local_thumbnails/...
    <base_path>/<tenant_server_name>/remote_content/...
    etc.

This ensures complete isolation of media files between tenants.
"""

import functools
import os
from typing import TYPE_CHECKING, Any, Callable, TypeVar, cast

from synapse.media.filepath import (
    ALLOWED_CHARACTERS,
    NEW_FORMAT_ID_RE,
    MediaFilePaths,
    _validate_path_component,
)
from synapse.tenant_context import get_current_tenant

if TYPE_CHECKING:
    from synapse.config.tenants import TenantConfig


F = TypeVar("F", bound=Callable[..., str])


def _wrap_in_tenant_base_path(func: F) -> F:
    """Takes a function that returns a relative path and turns it into an
    absolute path based on the location of the tenant's media store.

    This wrapper includes the tenant's server_name in the path to ensure
    isolation between tenants.
    """

    @functools.wraps(func)
    def _wrapped(self: "MultiTenantMediaFilePaths", *args: Any, **kwargs: Any) -> str:
        path = func(self, *args, **kwargs)
        tenant = get_current_tenant()
        if tenant is not None:
            # Use tenant-specific path
            tenant_dir = _validate_path_component(tenant.server_name)
            return os.path.join(self.base_path, tenant_dir, path)
        else:
            # Fall back to default path (non-multi-tenant mode)
            return os.path.join(self.base_path, path)

    return cast(F, _wrapped)


GetPathMethod = TypeVar(
    "GetPathMethod", bound=Callable[..., str] | Callable[..., list[str]]
)


def _wrap_with_tenant_jail_check(
    relative: bool,
) -> Callable[[GetPathMethod], GetPathMethod]:
    """Wraps a path-returning method to check that the returned path(s) do not escape
    the tenant's media store directory.

    Similar to the original _wrap_with_jail_check but tenant-aware.
    """

    def _wrap_with_jail_check_inner(func: GetPathMethod) -> GetPathMethod:
        @functools.wraps(func)
        def _wrapped(
            self: "MultiTenantMediaFilePaths", *args: Any, **kwargs: Any
        ) -> str | list[str]:
            path_or_paths = func(self, *args, **kwargs)

            if isinstance(path_or_paths, list):
                paths_to_check = path_or_paths
            else:
                paths_to_check = [path_or_paths]

            tenant = get_current_tenant()

            for path in paths_to_check:
                if relative:
                    if tenant is not None:
                        tenant_dir = _validate_path_component(tenant.server_name)
                        path = os.path.join(self.base_path, tenant_dir, path)
                    else:
                        path = os.path.join(self.base_path, path)

                normalized_path = os.path.normpath(path)

                # Determine the base path to check against
                if tenant is not None:
                    tenant_dir = _validate_path_component(tenant.server_name)
                    tenant_base = os.path.normpath(
                        os.path.join(self.base_path, tenant_dir)
                    )
                    check_base = tenant_base
                else:
                    check_base = self.normalized_base_path

                if os.path.commonpath([normalized_path, check_base]) != check_base:
                    raise ValueError(f"Invalid media store path: {path!r}")

            return path_or_paths

        return cast(GetPathMethod, _wrapped)

    return _wrap_with_jail_check_inner


class MultiTenantMediaFilePaths(MediaFilePaths):
    """Tenant-aware media file paths.

    This class extends MediaFilePaths to add tenant isolation. All media paths
    are prefixed with the current tenant's server_name to ensure complete
    isolation between tenants.

    The path structure becomes:
        <base_path>/<tenant_server_name>/local_content/<aa>/<bb>/<media_id>
        <base_path>/<tenant_server_name>/local_thumbnails/<aa>/<bb>/<media_id>/...
        etc.

    When no tenant context is set (non-multi-tenant mode), paths fall back to
    the original structure without the tenant prefix.
    """

    def __init__(self, primary_base_path: str):
        super().__init__(primary_base_path)

    def _get_tenant_base_path(self) -> str:
        """Get the base path for the current tenant."""
        tenant = get_current_tenant()
        if tenant is not None:
            tenant_dir = _validate_path_component(tenant.server_name)
            return os.path.join(self.base_path, tenant_dir)
        return self.base_path

    def _get_tenant_normalized_base_path(self) -> str:
        """Get the normalized base path for the current tenant."""
        return os.path.normpath(self._get_tenant_base_path())

    # Override local media methods with tenant-aware versions

    @_wrap_with_tenant_jail_check(relative=True)
    def local_media_filepath_rel(self, media_id: str) -> str:
        return os.path.join(
            "local_content",
            _validate_path_component(media_id[0:2]),
            _validate_path_component(media_id[2:4]),
            _validate_path_component(media_id[4:]),
        )

    local_media_filepath = _wrap_in_tenant_base_path(local_media_filepath_rel)

    @_wrap_with_tenant_jail_check(relative=True)
    def local_media_thumbnail_rel(
        self, media_id: str, width: int, height: int, content_type: str, method: str
    ) -> str:
        top_level_type, sub_type = content_type.split("/")
        file_name = "%i-%i-%s-%s-%s" % (width, height, top_level_type, sub_type, method)
        return os.path.join(
            "local_thumbnails",
            _validate_path_component(media_id[0:2]),
            _validate_path_component(media_id[2:4]),
            _validate_path_component(media_id[4:]),
            _validate_path_component(file_name),
        )

    local_media_thumbnail = _wrap_in_tenant_base_path(local_media_thumbnail_rel)

    @_wrap_with_tenant_jail_check(relative=False)
    def local_media_thumbnail_dir(self, media_id: str) -> str:
        """
        Retrieve the tenant-specific local store path of thumbnails of a given media_id

        Args:
            media_id: The media ID to query.
        Returns:
            Path of local_thumbnails from media_id within the tenant's directory
        """
        tenant_base = self._get_tenant_base_path()
        return os.path.join(
            tenant_base,
            "local_thumbnails",
            _validate_path_component(media_id[0:2]),
            _validate_path_component(media_id[2:4]),
            _validate_path_component(media_id[4:]),
        )

    # Override remote media methods with tenant-aware versions

    @_wrap_with_tenant_jail_check(relative=True)
    def remote_media_filepath_rel(self, server_name: str, file_id: str) -> str:
        return os.path.join(
            "remote_content",
            _validate_path_component(server_name),
            _validate_path_component(file_id[0:2]),
            _validate_path_component(file_id[2:4]),
            _validate_path_component(file_id[4:]),
        )

    remote_media_filepath = _wrap_in_tenant_base_path(remote_media_filepath_rel)

    @_wrap_with_tenant_jail_check(relative=True)
    def remote_media_thumbnail_rel(
        self,
        server_name: str,
        file_id: str,
        width: int,
        height: int,
        content_type: str,
        method: str,
    ) -> str:
        top_level_type, sub_type = content_type.split("/")
        file_name = "%i-%i-%s-%s-%s" % (width, height, top_level_type, sub_type, method)
        return os.path.join(
            "remote_thumbnail",
            _validate_path_component(server_name),
            _validate_path_component(file_id[0:2]),
            _validate_path_component(file_id[2:4]),
            _validate_path_component(file_id[4:]),
            _validate_path_component(file_name),
        )

    remote_media_thumbnail = _wrap_in_tenant_base_path(remote_media_thumbnail_rel)

    @_wrap_with_tenant_jail_check(relative=True)
    def remote_media_thumbnail_rel_legacy(
        self, server_name: str, file_id: str, width: int, height: int, content_type: str
    ) -> str:
        top_level_type, sub_type = content_type.split("/")
        file_name = "%i-%i-%s-%s" % (width, height, top_level_type, sub_type)
        return os.path.join(
            "remote_thumbnail",
            _validate_path_component(server_name),
            _validate_path_component(file_id[0:2]),
            _validate_path_component(file_id[2:4]),
            _validate_path_component(file_id[4:]),
            _validate_path_component(file_name),
        )

    @_wrap_with_tenant_jail_check(relative=False)
    def remote_media_thumbnail_dir(self, server_name: str, file_id: str) -> str:
        tenant_base = self._get_tenant_base_path()
        return os.path.join(
            tenant_base,
            "remote_thumbnail",
            _validate_path_component(server_name),
            _validate_path_component(file_id[0:2]),
            _validate_path_component(file_id[2:4]),
            _validate_path_component(file_id[4:]),
        )

    # Override URL cache methods with tenant-aware versions

    @_wrap_with_tenant_jail_check(relative=True)
    def url_cache_filepath_rel(self, media_id: str) -> str:
        if NEW_FORMAT_ID_RE.match(media_id):
            return os.path.join(
                "url_cache",
                _validate_path_component(media_id[:10]),
                _validate_path_component(media_id[11:]),
            )
        else:
            return os.path.join(
                "url_cache",
                _validate_path_component(media_id[0:2]),
                _validate_path_component(media_id[2:4]),
                _validate_path_component(media_id[4:]),
            )

    url_cache_filepath = _wrap_in_tenant_base_path(url_cache_filepath_rel)

    @_wrap_with_tenant_jail_check(relative=False)
    def url_cache_filepath_dirs_to_delete(self, media_id: str) -> list[str]:
        """The dirs to try and remove if we delete the media_id file"""
        tenant_base = self._get_tenant_base_path()
        if NEW_FORMAT_ID_RE.match(media_id):
            return [
                os.path.join(
                    tenant_base, "url_cache", _validate_path_component(media_id[:10])
                )
            ]
        else:
            return [
                os.path.join(
                    tenant_base,
                    "url_cache",
                    _validate_path_component(media_id[0:2]),
                    _validate_path_component(media_id[2:4]),
                ),
                os.path.join(
                    tenant_base, "url_cache", _validate_path_component(media_id[0:2])
                ),
            ]

    @_wrap_with_tenant_jail_check(relative=True)
    def url_cache_thumbnail_rel(
        self, media_id: str, width: int, height: int, content_type: str, method: str
    ) -> str:
        top_level_type, sub_type = content_type.split("/")
        file_name = "%i-%i-%s-%s-%s" % (width, height, top_level_type, sub_type, method)

        if NEW_FORMAT_ID_RE.match(media_id):
            return os.path.join(
                "url_cache_thumbnails",
                _validate_path_component(media_id[:10]),
                _validate_path_component(media_id[11:]),
                _validate_path_component(file_name),
            )
        else:
            return os.path.join(
                "url_cache_thumbnails",
                _validate_path_component(media_id[0:2]),
                _validate_path_component(media_id[2:4]),
                _validate_path_component(media_id[4:]),
                _validate_path_component(file_name),
            )

    url_cache_thumbnail = _wrap_in_tenant_base_path(url_cache_thumbnail_rel)

    @_wrap_with_tenant_jail_check(relative=True)
    def url_cache_thumbnail_directory_rel(self, media_id: str) -> str:
        if NEW_FORMAT_ID_RE.match(media_id):
            return os.path.join(
                "url_cache_thumbnails",
                _validate_path_component(media_id[:10]),
                _validate_path_component(media_id[11:]),
            )
        else:
            return os.path.join(
                "url_cache_thumbnails",
                _validate_path_component(media_id[0:2]),
                _validate_path_component(media_id[2:4]),
                _validate_path_component(media_id[4:]),
            )

    url_cache_thumbnail_directory = _wrap_in_tenant_base_path(
        url_cache_thumbnail_directory_rel
    )

    @_wrap_with_tenant_jail_check(relative=False)
    def url_cache_thumbnail_dirs_to_delete(self, media_id: str) -> list[str]:
        """The dirs to try and remove if we delete the media_id thumbnails"""
        tenant_base = self._get_tenant_base_path()
        if NEW_FORMAT_ID_RE.match(media_id):
            return [
                os.path.join(
                    tenant_base,
                    "url_cache_thumbnails",
                    _validate_path_component(media_id[:10]),
                    _validate_path_component(media_id[11:]),
                ),
                os.path.join(
                    tenant_base,
                    "url_cache_thumbnails",
                    _validate_path_component(media_id[:10]),
                ),
            ]
        else:
            return [
                os.path.join(
                    tenant_base,
                    "url_cache_thumbnails",
                    _validate_path_component(media_id[0:2]),
                    _validate_path_component(media_id[2:4]),
                    _validate_path_component(media_id[4:]),
                ),
                os.path.join(
                    tenant_base,
                    "url_cache_thumbnails",
                    _validate_path_component(media_id[0:2]),
                    _validate_path_component(media_id[2:4]),
                ),
                os.path.join(
                    tenant_base,
                    "url_cache_thumbnails",
                    _validate_path_component(media_id[0:2]),
                ),
            ]


def create_media_file_paths(
    primary_base_path: str, multi_tenant_enabled: bool = False
) -> MediaFilePaths:
    """Factory function to create the appropriate MediaFilePaths instance.

    Args:
        primary_base_path: The base path for media storage.
        multi_tenant_enabled: Whether multi-tenant mode is enabled.

    Returns:
        A MediaFilePaths instance (tenant-aware if multi-tenant is enabled).
    """
    if multi_tenant_enabled:
        return MultiTenantMediaFilePaths(primary_base_path)
    return MediaFilePaths(primary_base_path)
