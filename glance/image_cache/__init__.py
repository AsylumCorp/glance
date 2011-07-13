# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2011 OpenStack LLC.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
LRU Cache for Image Data
"""
from contextlib import contextmanager
import datetime
import errno
import logging
import os

import xattr

from glance.common import config

logger = logging.getLogger('glance.image_cache')


class ImageCache(object):
    """Provides an LRU cache for image data.

    Data is cached on READ not on WRITE; meaning if the cache is enabled, we
    attempt to read from the cache first, if we don't find the data, we begin
    streaming the data from the 'store' while simultaneously tee'ing the data
    into the cache. Subsequent reads will generate cache HITs for this image.

    Assumptions
    ===========

        1. Cache data directory exists on a filesytem that udpates atime on
           reads ('noatime' should NOT be set)

        2. Cache data directory exists on a filesystem that supports xattrs.
           This is optional, but highly recommended since it allows us to
           present ops with useful information pertaining to the cache, like
           human readable filenames and statistics.

        3. `glance-prune` is scheduled to run as a periodic job via cron. This
            is needed to run the LRU prune strategy to keep the cache size
            within the limits set by the config file.


    Cache Directory Notes
    =====================

    The image cache data directory contains the main cache path, where the
    active cache entries and two subdirectories for handling partial downloads
    and errored-out cache images.

    The layout looks like:
        
        image-cache/
            entry1
            entry2
            ...
            tmp/
            invalid/
    """
    def __init__(self, options):
        self.options = options
        self._make_cache_directory_if_needed()

    def _make_cache_directory_if_needed(self):
        """Creates main cache directory along with tmp subdirectory"""

        if self.enabled and not os.path.exists(self.tmp_path):
            logger.info("image cache directory doesn't exist, creating '%s'",
                        self.path)
            # NOTE(sirp): making the tmp_path will have the effect of creating
            # the main cache path directory as well
            os.makedirs(self.tmp_path)

    @property
    def enabled(self):
        return config.get_option(
            self.options, 'image_cache_enabled', type='bool', default=False)

    @property
    def path(self):
        """This is the base path for the image cache"""
        datadir = self.options['image_cache_datadir']
        return datadir

    @property
    def tmp_path(self):
        """This provides a temporary place to write our cache entries so that
        we we're not storing incomplete objects in the cache directly.

        When the file is finished writing to, it is moved from the tmp path
        back out into the main cache directory.

        The tmp_path is a subdirectory of the main cache path to ensure that
        they both reside on the same filesystem and thus making moves cheap.
        """
        return os.path.join(self.path, 'tmp')

    def path_for_image(self, image_meta):
        """This crafts an absolute path to a specific entry"""
        image_id = image_meta['id']
        return os.path.join(self.path, str(image_id))

    def tmp_path_for_image(self, image_meta):
        """This crafts an absolute path to a specific entry in the tmp
        directory
        """
        image_id = image_meta['id']
        return os.path.join(self.tmp_path, str(image_id))

    @contextmanager
    def open(self, image_meta, mode="r"):
        """Open a cache image for reading or writing.

        We have two possible scenarios:

            1. READ: we should attempt to read the file from the cache's
               main directory

            2. WRITE: we should write to a file under the cache's tmp
               directory, and when it's finished, move it out the main cache
               directory.
        """
        if 'w' in mode:
            with self._open_write(image_meta, mode) as cache_file:
                yield cache_file
        elif 'r' in mode:
            with self._open_read(image_meta, mode) as cache_file:
                yield cache_file
        else:
            raise Exception("mode '%s' not supported" % mode)

    @contextmanager
    def _open_write(self, image_meta, mode):
        tmp_path = self.tmp_path_for_image(image_meta)
        final_path = self.path_for_image(image_meta)

        def commit():
            os.rename(tmp_path, final_path)

        def rollback():
            # TODO(sirp): should we leave the file around for debugging
            # purposes
            os.unlink(tmp_path)

        # wrap in a transaction to make write atomic
        try:
            with open(tmp_path, mode) as cache_file:
                yield cache_file
        except:
            rollback()
            raise
        else:
            self._safe_set_xattr(tmp_path, 'image_name', image_meta['name'])
            #self._safe_set_xattr(tmp_path, 'hits', '0')
            try:
                commit()
            except:
                rollback()
                raise

    @contextmanager
    def _open_read(self, image_meta, mode):
        path = self.path_for_image(image_meta)
        with open(path, mode) as cache_file:
            yield cache_file

        #self._safe_increment_xattr(path, 'hits')

    @staticmethod
    def _safe_increment_xattr(path, key, n=1):
        """Safely increment an xattr field.

        NOTE(sirp): The 'safely', in this case, refers to the fact that the
        code will skip this step if xattrs isn't supported by the filesystem.

        Beware, this code *does* have a RACE CONDITION, since the
        read/update/write sequence is not atomic.

        For the most part, this is fine since we're just using this to collect
        interesting stats and not using the value to make critical decisions.

        Given that assumption, the added complexity and overhead of
        maintaining locks is not worth it.
        """
        try:
            count = int(self._safe_get_xattr(path, key))
        except KeyError:
            # NOTE(sirp): a KeyError is generated in two cases:
            # 1) xattrs is not supported by the filesystem
            # 2) the key is not present on the file
            #
            # In either case, just ignore it...
            pass
        else:
            # NOTE(sirp): only try to bump the count if xattrs is supported
            # and the key is present
            count += n
            self._safe_set_xattr(path, key, count)

    @staticmethod
    def _safe_set_xattr(path, key, value):
        """Set a xattr on the given path, skip if xattrs aren't supported"""
        entry_xattr = xattr.xattr(path)
        try:
            entry_xattr.set(key, value)
        except IOError as e:
            if e.errno == errno.EOPNOTSUPP:
                logger.warn("xattrs not supported, skipping...")
            else:
                raise

    @staticmethod
    def _safe_get_xattr(path, key, **kwargs):
        entry_xattr = xattr.xattr(path)
        try:
            return entry_xattr[key]
        except KeyError:
            if 'default' in kwargs:
                return kwargs['default']
            else:
                raise

    def hit(self, image_meta):
        path = self.path_for_image(image_meta)
        return os.path.exists(path)

    def purge(self, image_meta):
        path = self.path_for_image(image_meta)
        logger.debug("deleting image cache entry '%s'", path)
        if os.path.exists(path):
            os.unlink(path)

    def purge_all(self):
        for path in self.all_items():
            os.unlink(path)

    def all_items(self):
        for fname in os.listdir(self.path):
            path = os.path.join(self.path, fname)
            if os.path.isfile(path):
                yield path

    def entries(self):
        """Return cache info for each image that is cached"""
        entries = []
        for path in self.all_items():
            filename = os.path.basename(path)
            try:
                image_id = int(filename)
            except ValueError, TypeError:
                continue

            entry = {}
            entry['id'] = image_id
            entry['name'] = self._safe_get_xattr(
                path, 'image_name', default='UNKNOWN')
            #entry['hits'] = self._safe_get_xattr(
            #    path, 'hits', default='UNKNOWN')
            entry['size'] = os.path.getsize(path)

            accessed = os.path.getatime(path) or os.path.getmtime(path)
            last_accessed = datetime.datetime.fromtimestamp(accessed)\
                                             .isoformat()
            entry['last_accessed'] = last_accessed

            entries.append(entry)
        return entries
