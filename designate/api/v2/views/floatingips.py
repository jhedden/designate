# Copyright 2013 Hewlett-Packard Development Company, L.P.
#
# Author: Endre Karlson <endre.karlson@hp.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
from oslo_log import log as logging

from designate.api.v2.views import base as base_view


LOG = logging.getLogger(__name__)


class FloatingIPView(base_view.BaseView):
    """Model a FloatingIP PTR record as a python dict"""
    _resource_name = 'floatingip'
    _collection_name = 'floatingips'

    def _get_base_href(self, parents=None):
        return '%s/v2/reverse/floatingips' % self.base_uri

    def show_basic(self, context, request, item):
        item = item.to_dict()
        item['id'] = ":".join([item['region'], item['id']])
        del item['region']
        item['links'] = self._get_resource_links(
            request, item, [item['id']])
        return item
