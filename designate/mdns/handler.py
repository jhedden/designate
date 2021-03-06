# Copyright 2014 Hewlett-Packard Development Company, L.P.
#
# Author: Kiall Mac Innes <kiall@hp.com>
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
import dns
import dns.flags
import dns.opcode
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.message
from oslo.config import cfg
from oslo_log import log as logging

from designate import exceptions
from designate.mdns import xfr
from designate.central import rpcapi as central_api
from designate.i18n import _LI
from designate.i18n import _LW


LOG = logging.getLogger(__name__)
CONF = cfg.CONF

CONF.import_opt('default_pool_id', 'designate.central',
                group='service:central')


class RequestHandler(xfr.XFRMixin):

    def __init__(self, storage, tg):
        # Get a storage connection
        self.storage = storage
        self.tg = tg

    @property
    def central_api(self):
        return central_api.CentralAPI.get_instance()

    def __call__(self, request):
        """
        :param request: DNS Request Message
        :return: DNS Response Message
        """
        if request.opcode() == dns.opcode.QUERY:
            # Currently we expect exactly 1 question in the section
            # TSIG places the pseudo records into the additional section.
            if (len(request.question) != 1 or
                    request.question[0].rdclass != dns.rdataclass.IN):
                return self._handle_query_error(request, dns.rcode.REFUSED)

            q_rrset = request.question[0]
            # Handle AXFR and IXFR requests with an AXFR responses for now.
            # It is permissible for a server to send an AXFR response when
            # receiving an IXFR request.
            # TODO(Ron): send IXFR response when receiving IXFR request.
            if q_rrset.rdtype in (dns.rdatatype.AXFR, dns.rdatatype.IXFR):
                response = self._handle_axfr(request)
            else:
                response = self._handle_record_query(request)
        elif request.opcode() == dns.opcode.NOTIFY:
            response = self._handle_notify(request)
        else:
            # Unhandled OpCode's include STATUS, IQUERY, NOTIFY, UPDATE
            response = self._handle_query_error(request, dns.rcode.REFUSED)
        return response

    def _handle_notify(self, request):
        """
        Constructs the response to a NOTIFY and acts accordingly on it.

        * Checks if the master sending the NOTIFY is in the Zone's masters,
          if not it is ignored.
        * Checks if SOA query response serial != local serial.
        """
        context = request.environ['context']

        response = dns.message.make_response(request)

        if len(request.question) != 1:
            response.set_rcode(dns.rcode.FORMERR)
            return response
        else:
            question = request.question[0]

        criterion = {
            'name': question.name.to_text(),
            'type': 'SECONDARY',
            'deleted': False
        }

        try:
            domain = self.storage.find_domain(context, criterion)
        except exceptions.DomainNotFound:
            response.set_rcode(dns.rcode.NOTAUTH)
            return response

        notify_addr = request.environ['addr'][0]

        # We check if the src_master which is the assumed master for the zone
        # that is sending this NOTIFY OP is actually the master. If it's not
        # We'll reply but don't do anything with the NOTIFY.
        master_addr = domain.get_master_by_ip(notify_addr)
        if not master_addr:
            msg = _LW("NOTIFY for %(name)s from non-master server "
                      "%(addr)s, ignoring.")
            LOG.warn(msg % {"name": domain.name, "addr": notify_addr})
            response.set_rcode(dns.rcode.REFUSED)
            return response

        resolver = dns.resolver.Resolver()
        # According to RFC we should query the server that sent the NOTIFY
        resolver.nameservers = [notify_addr]

        soa_answer = resolver.query(domain.name, 'SOA')
        soa_serial = soa_answer[0].serial
        if soa_serial == domain.serial:
            msg = _LI("Serial %(serial)s is the same for master and us for "
                      "%(domain_id)s")
            LOG.info(msg % {"serial": soa_serial, "domain_id": domain.id})
        else:
            msg = _LI("Scheduling AXFR for %(domain_id)s from %(master_addr)s")
            info = {"domain_id": domain.id, "master_addr": master_addr}
            LOG.info(msg % info)
            self.tg.add_thread(self.domain_sync, context, domain,
                               [master_addr])

        response.flags |= dns.flags.AA

        return response

    def _handle_query_error(self, request, rcode):
        """
        Construct an error response with the rcode passed in.
        :param request: The decoded request from the wire.
        :param rcode: The response code to send back.
        :return: A dns response message with the response code set to rcode
        """
        response = dns.message.make_response(request)
        response.set_rcode(rcode)

        return response

    def _domain_criterion_from_request(self, request, criterion=None):
        """Builds a bare criterion dict based on the request attributes"""
        criterion = criterion or {}

        tsigkey = request.environ.get('tsigkey')

        if tsigkey is None and CONF['service:mdns'].query_enforce_tsig:
            raise exceptions.Forbidden('Request is not TSIG signed')

        elif tsigkey is None:
            # Default to using the default_pool_id when no TSIG key is
            # available
            criterion['pool_id'] = CONF['service:central'].default_pool_id

        else:
            if tsigkey.scope == 'POOL':
                criterion['pool_id'] = tsigkey.resource_id

            elif tsigkey.scope == 'ZONE':
                criterion['id'] = tsigkey.resource_id

            else:
                raise NotImplementedError("Support for %s scoped TSIG Keys is "
                                          "not implemented")

        return criterion

    def _convert_to_rrset(self, domain, recordset):
        # Fetch the domain or the config ttl if the recordset ttl is null
        if recordset.ttl:
            ttl = recordset.ttl
        else:
            ttl = domain.ttl

        # construct rdata from all the records
        rdata = []
        for record in recordset.records:
            # TODO(Ron): this should be handled in the Storage query where we
            # find the recordsets.
            if record.action != 'DELETE':
                rdata.append(str(record.data))

        # Now put the records into dnspython's RRsets
        # answer section has 1 RR set.  If the RR set has multiple
        # records, DNSpython puts each record in a separate answer
        # section.
        # RRSet has name, ttl, class, type  and rdata
        # The rdata has one or more records
        r_rrset = None
        if rdata:
            r_rrset = dns.rrset.from_text_list(
                recordset.name, ttl, dns.rdataclass.IN, recordset.type, rdata)

        return r_rrset

    def _handle_axfr(self, request):
        context = request.environ['context']

        response = dns.message.make_response(request)
        q_rrset = request.question[0]
        # First check if there is an existing zone
        # TODO(vinod) once validation is separated from the api,
        # validate the parameters
        try:
            criterion = self._domain_criterion_from_request(
                request, {'name': q_rrset.name.to_text()})
            domain = self.storage.find_domain(context, criterion)

        except exceptions.DomainNotFound:
            LOG.warning(_LW("DomainNotFound while handling axfr request. "
                            "Question was %(qr)s") % {'qr': q_rrset})

            return self._handle_query_error(request, dns.rcode.REFUSED)

        except exceptions.Forbidden:
            LOG.warning(_LW("Forbidden while handling axfr request. "
                            "Question was %(qr)s") % {'qr': q_rrset})

            return self._handle_query_error(request, dns.rcode.REFUSED)

        r_rrsets = []

        # The AXFR response needs to have a SOA at the beginning and end.
        criterion = {'domain_id': domain.id, 'type': 'SOA'}
        soa_recordsets = self.storage.find_recordsets(context, criterion)

        for recordset in soa_recordsets:
            r_rrsets.append(self._convert_to_rrset(domain, recordset))

        # Get all the recordsets other than SOA
        criterion = {'domain_id': domain.id, 'type': '!SOA'}
        recordsets = self.storage.find_recordsets(context, criterion)

        for recordset in recordsets:
            r_rrset = self._convert_to_rrset(domain, recordset)
            if r_rrset:
                r_rrsets.append(r_rrset)

        # Append the SOA recordset at the end
        for recordset in soa_recordsets:
            r_rrsets.append(self._convert_to_rrset(domain, recordset))

        response.set_rcode(dns.rcode.NOERROR)
        # TODO(vinod) check if we dnspython has an upper limit on the number
        # of rrsets.
        response.answer = r_rrsets
        # For all the data stored in designate mdns is Authoritative
        response.flags |= dns.flags.AA

        return response

    def _handle_record_query(self, request):
        """Handle a DNS QUERY request for a record"""
        context = request.environ['context']
        response = dns.message.make_response(request)

        try:
            q_rrset = request.question[0]
            # TODO(vinod) once validation is separated from the api,
            # validate the parameters
            criterion = {
                'name': q_rrset.name.to_text(),
                'type': dns.rdatatype.to_text(q_rrset.rdtype),
                'domains_deleted': False
            }
            recordset = self.storage.find_recordset(context, criterion)

            try:
                criterion = self._domain_criterion_from_request(
                    request, {'id': recordset.domain_id})
                domain = self.storage.find_domain(context, criterion)

            except exceptions.DomainNotFound:
                LOG.warning(_LW("DomainNotFound while handling query request"
                                ". Question was %(qr)s") % {'qr': q_rrset})

                return self._handle_query_error(request, dns.rcode.REFUSED)

            except exceptions.Forbidden:
                LOG.warning(_LW("Forbidden while handling query request. "
                                "Question was %(qr)s") % {'qr': q_rrset})

                return self._handle_query_error(request, dns.rcode.REFUSED)

            r_rrset = self._convert_to_rrset(domain, recordset)
            response.set_rcode(dns.rcode.NOERROR)
            response.answer = [r_rrset]
            # For all the data stored in designate mdns is Authoritative
            response.flags |= dns.flags.AA

        except exceptions.NotFound:
            # If an FQDN exists, like www.rackspace.com, but the specific
            # record type doesn't exist, like type SPF, then the return code
            # would be NOERROR and the SOA record is returned.  This tells
            # caching nameservers that the FQDN does exist, so don't negatively
            # cache it, but the specific record doesn't exist.
            #
            # If an FQDN doesn't exist with any record type, that is NXDOMAIN.
            # However, an authoritative nameserver shouldn't return NXDOMAIN
            # for a zone it isn't authoritative for.  It would be more
            # appropriate for it to return REFUSED.  It should still return
            # NXDOMAIN if it is authoritative for a domain but the FQDN doesn't
            # exist, like abcdef.rackspace.com.  Of course, a wildcard within a
            # domain would mean that NXDOMAIN isn't ever returned for a domain.
            #
            # To simply things currently this returns a REFUSED in all cases.
            # If zone transfers needs different errors, we could revisit this.
            response.set_rcode(dns.rcode.REFUSED)

        except exceptions.Forbidden:
            response.set_rcode(dns.rcode.REFUSED)

        return response
