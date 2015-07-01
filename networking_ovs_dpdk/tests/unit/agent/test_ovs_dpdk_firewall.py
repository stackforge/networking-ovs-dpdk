# Copyright 2012, Nachi Ueno, NTT MCL, Inc.
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

import copy
import mock
import six

from networking_ovs_dpdk.agent import ovs_dpdk_firewall
from neutron.agent.common import config as a_cfg
from neutron.agent.common import ovs_lib
from neutron.agent import securitygroups_rpc as sg_cfg
from neutron.common import constants
from neutron.tests import base
from oslo_config import cfg

FAKE_PREFIX = {constants.IPv4: '10.0.0.0/24',
               constants.IPv6: 'fe80::/48'}
FAKE_IP = {constants.IPv4: '10.0.0.1',
           constants.IPv6: 'fe80::1'}

FAKE_SGID = 'fake_sgid'
OTHER_SGID = 'other_sgid'

# List of protocols.
PROTOCOLS = {'tcp': 'eth_type=0x0800,ip_proto=6',
             'udp': 'eth_type=0x0800,ip_proto=17',
             'ip': 'eth_type=0x0800',
             'icmp': 'eth_type=0x0800,ip_proto=1'}
PROTOCOLS_DEFAULT_PRIO = {'tcp': 70,
                          'udp': 70,
                          'ip': 60,
                          'icmp': 60}
PROTOCOLS_LEARN_ACTION_PRIO = {'tcp': 60,
                               'udp': 60,
                               'ip': 60,
                               'icmp': 60}
PROTOCOLS_DEST = {'tcp': 'NXM_OF_TCP_DST[]=NXM_OF_TCP_SRC[],',
                  'udp': 'NXM_OF_UDP_DST[]=NXM_OF_UDP_SRC[],',
                  'ip': '',
                  'icmp': ''}

PROTOCOLS_SRC = {'tcp': 'NXM_OF_TCP_SRC[]=NXM_OF_TCP_DST[],',
                 'udp': 'NXM_OF_UDP_SRC[]=NXM_OF_UDP_DST[],',
                 'ip': '',
                 'icmp': ''}

IDLE_TIMEOUT = 30
HARD_TIMEOUT = 1800

# From networking_ovs_dpdk.common.config
DEFAULT_BRIDGE_MAPPINGS = []
ovs_opts = [
    cfg.StrOpt('integration_bridge', default='br-int',
               help=_("Integration bridge to use.")),
    cfg.StrOpt('tunnel_bridge', default='br-tun',
               help=_("Tunnel bridge to use.")),
    cfg.StrOpt('int_peer_patch_port', default='patch-tun',
               help=_("Peer patch port in integration bridge for tunnel "
                      "bridge.")),
    cfg.StrOpt('tun_peer_patch_port', default='patch-int',
               help=_("Peer patch port in tunnel bridge for integration "
                      "bridge.")),
    cfg.IPOpt('local_ip', version=4,
              help=_("Local IP address of tunnel endpoint.")),
    cfg.ListOpt('bridge_mappings',
                default=DEFAULT_BRIDGE_MAPPINGS,
                help=_("List of <physical_network>:<bridge>. "
                       "Deprecated for ofagent.")),
    cfg.BoolOpt('use_veth_interconnection', default=False,
                help=_("Use veths instead of patch ports to interconnect the "
                       "integration bridge to physical bridges.")),
    cfg.StrOpt('of_interface', default='ovsdpdk-ofctl',
               choices=['ovs-ofctl', 'ovsdpdk-ofctl'],
               help=_("OpenFlow interface to use.")),
]


class BaseOVSDPDKFirewallTestCase(base.BaseTestCase):
    def setUp(self):
        super(BaseOVSDPDKFirewallTestCase, self).setUp()
        cfg.CONF.register_opts(a_cfg.ROOT_HELPER_OPTS, 'AGENT')
        cfg.CONF.register_opts(sg_cfg.security_group_opts, 'SECURITYGROUP')
        cfg.CONF.register_opts(sg_cfg.security_group_opts, 'OVS')
        cfg.CONF.register_opts(ovs_opts, "OVS")
        self.firewall = ovs_dpdk_firewall.OVSFirewallDriver()


class OVSDPDKFirewallTestCase(BaseOVSDPDKFirewallTestCase):
    def setUp(self):
        super(OVSDPDKFirewallTestCase, self).setUp()
        self._mock_add_flow = \
            mock.patch.object(ovs_lib.OVSBridge, "add_flow")
        self.mock_add_flow = self._mock_add_flow.start()
        self._mock_delete_flows = \
            mock.patch.object(ovs_lib.OVSBridge, "delete_flows")
        self.mock_delete_flows = self._mock_delete_flows.start()
        self._mock_get_vif_port_by_id = \
            mock.patch.object(ovs_lib.OVSBridge, "get_vif_port_by_id")
        self.mock_get_vif_port_by_id = self._mock_get_vif_port_by_id.start()

        # Create a fake port.
        self.fake_port_1 = self._fake_port(name='tapfake_dev_1')
        # Mock the VifPort.
        self.mock_get_vif_port_by_id.return_value = \
            self._fake_vifport(self.fake_port_1)

    def tearDown(self):
        super(OVSDPDKFirewallTestCase, self).tearDown()
        self._mock_add_flow.stop()
        self._mock_delete_flows.stop()
        self._mock_get_vif_port_by_id.stop()

    def _fake_port(self, name,
                   ofport=1,
                   device='tapfake_dev_1',
                   mac='ff:ff:ff:ff:ff:ff',
                   sg_id=FAKE_SGID,
                   zone_id=1):
        return {'name': name,
                'ofport': ofport,
                'device': device,
                'mac_address': mac,
                'zone_id': zone_id,
                'network_id': 'fake_net',
                'fixed_ips': [FAKE_IP[constants.IPv4],
                              FAKE_IP[constants.IPv6]],
                'security_groups': [sg_id],
                'security_group_source_groups': [sg_id]}

    def _fake_sg_rule_for_ethertype(self, ethertype, remote_group):
        return {'direction': 'ingress', 'remote_group_id': remote_group,
                'ethertype': ethertype}

    def _fake_sg_rules(self, sg_id=FAKE_SGID, remote_groups=None):
        remote_groups = remote_groups or {constants.IPv4: [FAKE_SGID],
                                          constants.IPv6: [FAKE_SGID]}
        rules = []
        for ip_version, remote_group_list in six.iteritems(remote_groups):
            for remote_group in remote_group_list:
                rules.append(self._fake_sg_rule_for_ethertype(ip_version,
                                                              remote_group))
        return {sg_id: rules}

    def _fake_sg_members(self, sg_ids=None):
        return {sg_id: copy.copy(FAKE_IP)
                for sg_id in (sg_ids or [FAKE_SGID])}

    def _fake_vifport(self, port):
        return ovs_lib.VifPort(port['name'],
                               port['ofport'],
                               port['device'],
                               port['mac_address'],
                               "br-%s" % port['device'])

    def _learn_egress_actions(self, protocol, priority=None,
                       icmp_type=None, icmp_code=None):
        protocol_str = PROTOCOLS[protocol]
        if not priority:
            priority = PROTOCOLS_DEFAULT_PRIO[protocol]
        port_destination = PROTOCOLS_DEST[protocol]
        port_source = PROTOCOLS_SRC[protocol]
        icmp_type_str = ""
        if icmp_type:
            icmp_type_str = 'icmp_type=%s,' % icmp_type
        icmp_code_str = ""
        if icmp_code:
            icmp_code_str = 'icmp_code=%s,' % icmp_code
        output_str = 'learn(table=12,priority=%(priority)s,' \
                     'idle_timeout=%(idle_timeout)s,' \
                     'hard_timeout=%(hard_timeout)s,' \
                     '%(protocol)s,' \
                     'NXM_OF_ETH_SRC[]=NXM_OF_ETH_DST[],' \
                     'NXM_OF_ETH_DST[]=NXM_OF_ETH_SRC[],' \
                     'NXM_OF_IP_SRC[]=NXM_OF_IP_DST[],' \
                     'NXM_OF_IP_DST[]=NXM_OF_IP_SRC[],' \
                     '%(port_destination)s' \
                     '%(port_source)s' \
                     '%(icmp_type)s' \
                     '%(icmp_code)s' \
                     'output:NXM_OF_IN_PORT[]),' \
                     'resubmit(,2)' % {'priority': priority,
                                       'idle_timeout': IDLE_TIMEOUT,
                                       'hard_timeout': HARD_TIMEOUT,
                                       'protocol': protocol_str,
                                       'port_destination': port_destination,
                                       'port_source': port_source,
                                       'icmp_type': icmp_type_str,
                                       'icmp_code': icmp_code_str}
        return output_str

    def _learn_ingress_actions(self, protocol, priority=None,
                       icmp_type=None, icmp_code=None, ofport=1):
        protocol_str = PROTOCOLS[protocol]
        if not priority:
            priority = PROTOCOLS_DEFAULT_PRIO[protocol]
        port_destination = PROTOCOLS_DEST[protocol]
        port_source = PROTOCOLS_SRC[protocol]
        icmp_type_str = ""
        if icmp_type:
            icmp_type_str = 'icmp_type=%s,' % icmp_type
        icmp_code_str = ""
        if icmp_code:
            icmp_code_str = 'icmp_code=%s,' % icmp_code
        output_str = 'learn(table=11,priority=%(priority)s,' \
                     'idle_timeout=%(idle_timeout)s,' \
                     'hard_timeout=%(hard_timeout)s,' \
                     '%(protocol)s,' \
                     'NXM_OF_ETH_SRC[]=NXM_OF_ETH_DST[],' \
                     'NXM_OF_ETH_DST[]=NXM_OF_ETH_SRC[],' \
                     'NXM_OF_IP_SRC[]=NXM_OF_IP_DST[],' \
                     'NXM_OF_IP_DST[]=NXM_OF_IP_SRC[],' \
                     '%(port_destination)s' \
                     '%(port_source)s' \
                     '%(icmp_type)s' \
                     '%(icmp_code)s' \
                     'output:NXM_OF_IN_PORT[]),' \
                     'strip_vlan,output:%(ofport)s' % {'priority': priority,
                                       'idle_timeout': IDLE_TIMEOUT,
                                       'hard_timeout': HARD_TIMEOUT,
                                       'protocol': protocol_str,
                                       'port_destination': port_destination,
                                       'port_source': port_source,
                                       'icmp_type': icmp_type_str,
                                       'icmp_code': icmp_code_str,
                                       'ofport': ofport}
        return output_str

    def test_port_rule_masking(self):
        # Compare elements in two list, using sets and the length (sets don't
        # have duplicated elements).
        compare_rules = lambda x, y: set(x) == set(y) and len(x) == len(y)

        # Test 1.
        port_max = 12
        port_min = 5
        expected_rules = ['0x0005', '0x000c', '0x0006/0xfffe',
                          '0x0008/0xfffc']
        rules = self.firewall._port_rule_masking(port_min, port_max)
        assert compare_rules(rules, expected_rules), \
            "Expected rules: %s\n" \
            "Calculated rules: %s" % (expected_rules, rules)

        # Test 2.
        port_max = 130
        port_min = 20
        expected_rules = ['0x0014/0xfffe', '0x0016/0xfffe', '0x0018/0xfff8',
                          '0x0020/0xffe0', '0x0040/0xffc0', '0x0080/0xfffe',
                          '0x0082']
        rules = self.firewall._port_rule_masking(port_min, port_max)
        assert rules == expected_rules, \
            "Expected rules: %s\n" \
            "Calculated rules: %s" % (expected_rules, rules)

        # Test 3.
        port_max = 33057
        port_min = 4501
        expected_rules = ['0x1195', '0x1196/0xfffe', '0x1198/0xfff8',
                          '0x11a0/0xffe0', '0x11c0/0xffc0', '0x1200/0xfe00',
                          '0x1400/0xfc00', '0x1800/0xf800', '0x2000/0xe000',
                          '0x4000/0xc000', '0x8021/0xff00', '0x8101/0xffe0',
                          '0x8120/0xfffe']

        rules = self.firewall._port_rule_masking(port_min, port_max)
        assert rules == expected_rules,\
            "Expected rules: %s\n" \
            "Calculated rules: %s" % (expected_rules, rules)

    @mock.patch.object(ovs_dpdk_firewall.OVSFirewallDriver, "_outbound_port")
    def test_prepare_port_filter(self, mock_outbond_port):
        # Setup rules and SG.
        self.firewall.sg_rules = self._fake_sg_rules()
        self.firewall.sg_members = {FAKE_SGID: {
            constants.IPv4: ['10.0.0.1', '10.0.0.2'],
            constants.IPv6: ['fe80::1']}}
        self.firewall.pre_sg_members = {}
        port = self.fake_port_1
        # Mock the outbond port.
        outbond_port = 100
        mock_outbond_port.return_value = outbond_port
        self.firewall.prepare_port_filter(port)

        calls_del_flows = [mock.call(dl_src=port['mac_address']),
                           mock.call(dl_dst=port['mac_address']),
                           mock.call(in_port=port['ofport'])]
        self.mock_delete_flows.assert_has_calls(calls_del_flows,
                                                any_order=False)

        self.firewall._filtered_ports = port

        calls_add_flows = [
            mock.call(actions='goto_table:1',
                      dl_src=port['mac_address'], in_port=port['ofport'],
                      nw_src='0.0.0.0', priority=100,
                      proto='ip'),
            mock.call(actions='mod_vlan_vid:%s,goto_table:1' % port['zone_id'],
                      dl_src=port['mac_address'], in_port=port['ofport'],
                      nw_src='10.0.0.1', priority=100,
                      proto='ip'),
            mock.call(actions='drop', in_port=port['ofport'], priority=40,
                      proto='udp', table=11, udp_dst=68, udp_src=67),
            mock.call(actions='drop', in_port=port['ofport'], priority=40,
                      proto='udp', table=11, udp_dst=546, udp_src=547),
            mock.call(actions='normal', dl_src=port['mac_address'],
                      in_port=port['ofport'], priority=50, proto='udp',
                      table=11, udp_dst=67, udp_src=68),
            mock.call(actions='normal', dl_src=port['mac_address'],
                      in_port=port['ofport'], priority=50, proto='udp',
                      table=11, udp_dst=547, udp_src=546),
            mock.call(actions='normal', dl_src=port['mac_address'],
                      in_port=port['ofport'], priority=50, proto='icmp',
                      table=11),
            mock.call(actions='normal', dl_src=port['mac_address'],
                      in_port=port['ofport'], priority=50,
                      proto='ipv6,nw_proto=58', table=11),
            mock.call(actions='drop', priority=40, proto='ip'),
            mock.call(actions='strip_vlan,output:%s' % port['ofport'],
                      dl_dst=port['mac_address'], priority=60, proto='arp'),
            mock.call(actions='resubmit(0,2)', dl_dst=port['mac_address'],
                      priority=50),
            mock.call(actions='strip_vlan,output:%s' % port['ofport'],
                       dl_dst=port['mac_address'], priority=45,
                       udp_dst=68, udp_src=67, proto='udp', table=12),
            mock.call(actions='strip_vlan,output:%s' % port['ofport'],
                      dl_dst=port['mac_address'], priority=45,
                      udp_dst=546, udp_src=547, proto='udp', table=12),
            mock.call(actions='strip_vlan,output:%s' % port['ofport'],
                      dl_dst=port['mac_address'], icmp_type=130, priority=45,
                      proto='ipv6,nw_proto=58', table=12),
            mock.call(actions='strip_vlan,output:%s' % port['ofport'],
                      dl_dst=port['mac_address'], icmp_type=131, priority=45,
                      proto='ipv6,nw_proto=58', table=12),
            mock.call(actions='strip_vlan,output:%s' % port['ofport'],
                      dl_dst=port['mac_address'], icmp_type=132, priority=45,
                      proto='ipv6,nw_proto=58', table=12),
            mock.call(actions='strip_vlan,output:%s' % port['ofport'],
                      dl_dst=port['mac_address'], icmp_type=135, priority=45,
                      proto='ipv6,nw_proto=58', table=12),
            mock.call(actions='strip_vlan,output:%s' % port['ofport'],
                      dl_dst=port['mac_address'], icmp_type=136, priority=45,
                      proto='ipv6,nw_proto=58', table=12),
            mock.call(actions='mod_vlan_vid:%s,output:%s' %
                              (port['zone_id'], outbond_port),
                      proto='ip', priority=10, table=12),
            mock.call(actions='strip_vlan,resubmit(,12)',
                      dl_dst=port['mac_address'], priority=100, table=2),
            mock.call(actions='resubmit(,12)', priority=90, table=2),
            mock.call(actions='strip_vlan,resubmit(,11)',
                      dl_dst=port['mac_address'], priority=100, table=1),
            mock.call(actions='resubmit(,11)', priority=90, table=1)]
        self.mock_add_flow.assert_has_calls(calls_add_flows, any_order=False)

    def _test_rules(self, rule_list, fake_sgid, flow_call_list):
        self.firewall.update_security_group_rules(FAKE_SGID, rule_list)
        self.firewall._add_rules_flows(self.fake_port_1)

        calls_add_flows = flow_call_list
        self.mock_add_flow.assert_has_calls(calls_add_flows, any_order=False)

    def test_filter_ipv4_ingress(self):
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress'}
        flow_call_list = []
        for proto in ['tcp', 'udp', 'ip']:
            priority = PROTOCOLS_DEFAULT_PRIO[proto]
            flow_call_list.append(mock.call(
                actions=self._learn_ingress_actions(proto, priority,
                                 ofport=self.fake_port_1['ofport']),
                dl_dst=self.fake_port_1['mac_address'],
                nw_dst=self.fake_port_1['fixed_ips'][0],
                priority=30,
                proto=proto,
                table=12))
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_prefix(self):
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'source_ip_prefix': prefix}
        flow_call_list = []
        for proto in ['tcp', 'udp', 'ip']:
            priority = PROTOCOLS_DEFAULT_PRIO[proto]
            flow_call_list.append(mock.call(
                actions=self._learn_ingress_actions(proto, priority,
                                 ofport=self.fake_port_1['ofport']),
                dl_dst=self.fake_port_1['mac_address'],
                nw_dst=self.fake_port_1['fixed_ips'][0],
                nw_src=prefix,
                priority=30,
                proto=proto,
                table=12))
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_tcp(self):
        proto = 'tcp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': proto}
        flow_call_list = [mock.call(
            actions=self._learn_ingress_actions(proto, priority,
                ofport=self.fake_port_1['ofport']),
            dl_dst=self.fake_port_1['mac_address'],
            nw_dst=self.fake_port_1['fixed_ips'][0],
            priority=30,
            proto=proto,
            table=12)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_tcp_prefix(self):
        proto = 'tcp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': proto,
                'source_ip_prefix': prefix}
        flow_call_list = [mock.call(
            actions=self._learn_ingress_actions(proto, priority,
                ofport=self.fake_port_1['ofport']),
            dl_dst=self.fake_port_1['mac_address'],
            nw_dst=self.fake_port_1['fixed_ips'][0],
            nw_src=prefix,
            priority=30,
            proto=proto,
            table=12)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_icmp(self):
        proto = 'icmp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        icmp_type = 10
        icmp_code = 20
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': proto,
                'port_range_min': icmp_type,
                'port_range_max': icmp_code,
                'source_ip_prefix': prefix}
        flow_call_list = [mock.call(
            actions=self._learn_ingress_actions(proto, priority,
                ofport=self.fake_port_1['ofport'], icmp_type=icmp_type,
                icmp_code=icmp_code),
            dl_dst=self.fake_port_1['mac_address'],
            nw_dst=self.fake_port_1['fixed_ips'][0],
            nw_src=prefix,
            priority=30,
            proto=proto,
            table=12)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_icmp_prefix(self):
        proto = 'icmp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        icmp_type = 10
        icmp_code = 20
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': proto,
                'port_range_min': icmp_type,
                'port_range_max': icmp_code,
                'source_ip_prefix': prefix}
        flow_call_list = [mock.call(
            actions=self._learn_ingress_actions(proto, priority,
                ofport=self.fake_port_1['ofport'], icmp_type=icmp_type,
                icmp_code=icmp_code),
            dl_dst=self.fake_port_1['mac_address'],
            nw_dst=self.fake_port_1['fixed_ips'][0],
            nw_src=prefix,
            priority=30,
            proto=proto,
            table=12)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_tcp_port(self):
        proto = 'tcp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': proto,
                'port_range_min': 10,
                'port_range_max': 10}
        flow_call_list = [mock.call(
            actions=self._learn_ingress_actions(proto, priority,
                ofport=self.fake_port_1['ofport']),
            dl_dst=self.fake_port_1['mac_address'],
            nw_dst=self.fake_port_1['fixed_ips'][0],
            tcp_dst=rule['port_range_min'],
            priority=30,
            proto=proto,
            table=12)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_tcp_mport(self):
        proto = 'tcp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': proto,
                'port_range_min': 10,
                'port_range_max': 100}
        tcp_dst = ['0x000a/0xfffe', '0x000c/0xfffc', '0x0010/0xfff0',
                   '0x0020/0xffe0', '0x0044/0xffe0', '0x0060/0xfffc',
                   '0x0064']
        flow_call_list = []
        for port in tcp_dst:
            flow_call_list.append(mock.call(
                actions=self._learn_ingress_actions(proto, priority,
                    ofport=self.fake_port_1['ofport']),
                dl_dst=self.fake_port_1['mac_address'],
                nw_dst=self.fake_port_1['fixed_ips'][0],
                tcp_dst=port,
                priority=30,
                proto=proto,
                table=12))
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_tcp_mport_prefix(self):
        proto = 'tcp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': 'tcp',
                'port_range_min': 10,
                'port_range_max': 100,
                'source_ip_prefix': prefix}
        tcp_dst = ['0x000a/0xfffe', '0x000c/0xfffc', '0x0010/0xfff0',
                   '0x0020/0xffe0', '0x0044/0xffe0', '0x0060/0xfffc',
                   '0x0064']
        flow_call_list = []
        for port in tcp_dst:
            flow_call_list.append(mock.call(
                actions=self._learn_ingress_actions(proto, priority,
                    ofport=self.fake_port_1['ofport']),
                dl_dst=self.fake_port_1['mac_address'],
                nw_dst=self.fake_port_1['fixed_ips'][0],
                nw_src=prefix,
                tcp_dst=port,
                priority=30,
                proto=proto,
                table=12))
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_udp(self):
        proto = 'udp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': proto}
        flow_call_list = [mock.call(
            actions=self._learn_ingress_actions(proto, priority,
                ofport=self.fake_port_1['ofport']),
            dl_dst=self.fake_port_1['mac_address'],
            nw_dst=self.fake_port_1['fixed_ips'][0],
            priority=30,
            proto=proto,
            table=12)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_udp_prefix(self):
        proto = 'udp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': proto,
                'source_ip_prefix': prefix}
        flow_call_list = [mock.call(
            actions=self._learn_ingress_actions(proto, priority,
                ofport=self.fake_port_1['ofport']),
            dl_dst=self.fake_port_1['mac_address'],
            nw_dst=self.fake_port_1['fixed_ips'][0],
            nw_src=prefix,
            priority=30,
            proto=proto,
            table=12)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_udp_port(self):
        proto = 'udp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': 'udp',
                'port_range_min': 10,
                'port_range_max': 10}
        flow_call_list = [mock.call(
            actions=self._learn_ingress_actions(proto, priority,
                ofport=self.fake_port_1['ofport']),
            dl_dst=self.fake_port_1['mac_address'],
            nw_dst=self.fake_port_1['fixed_ips'][0],
            priority=30,
            udp_dst=10,
            proto=proto,
            table=12)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_udp_mport(self):
        proto = 'udp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': 'udp',
                'port_range_min': 10,
                'port_range_max': 100}
        udp_dst = ['0x000a/0xfffe', '0x000c/0xfffc', '0x0010/0xfff0',
                   '0x0020/0xffe0', '0x0044/0xffe0', '0x0060/0xfffc',
                   '0x0064']
        flow_call_list = []
        for port in udp_dst:
            flow_call_list.append(mock.call(
                actions=self._learn_ingress_actions(proto, priority,
                    ofport=self.fake_port_1['ofport']),
                dl_dst=self.fake_port_1['mac_address'],
                nw_dst=self.fake_port_1['fixed_ips'][0],
                udp_dst=port,
                priority=30,
                proto=proto,
                table=12))
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_ingress_udp_mport_prefix(self):
        proto = 'udp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'ingress',
                'protocol': 'udp',
                'port_range_min': 10,
                'port_range_max': 100,
                'source_ip_prefix': prefix}
        udp_dst = ['0x000a/0xfffe', '0x000c/0xfffc', '0x0010/0xfff0',
                   '0x0020/0xffe0', '0x0044/0xffe0', '0x0060/0xfffc',
                   '0x0064']
        flow_call_list = []
        for port in udp_dst:
            flow_call_list.append(mock.call(
                actions=self._learn_ingress_actions(proto, priority,
                    ofport=self.fake_port_1['ofport']),
                dl_dst=self.fake_port_1['mac_address'],
                nw_dst=self.fake_port_1['fixed_ips'][0],
                nw_src=prefix,
                udp_dst=port,
                priority=30,
                proto=proto,
                table=12))
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress(self):
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress'}
        flow_call_list = []
        for proto in ['tcp', 'udp', 'ip']:
            priority = PROTOCOLS_DEFAULT_PRIO[proto]
            flow_call_list.append(
                mock.call(actions=self._learn_egress_actions(proto,
                                  priority),
                          dl_src=self.fake_port_1['mac_address'],
                          nw_src=self.fake_port_1['fixed_ips'][0],
                          priority=30,
                          proto=proto,
                          table=11))
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_prefix(self):
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'dest_ip_prefix': prefix}
        flow_call_list = []
        for proto in ['tcp', 'udp', 'ip']:
            priority = PROTOCOLS_DEFAULT_PRIO[proto]
            flow_call_list.append(
                mock.call(actions=self._learn_egress_actions(proto,
                                  priority),
                          dl_src=self.fake_port_1['mac_address'],
                          nw_src=self.fake_port_1['fixed_ips'][0],
                          nw_dst=prefix,
                          priority=30,
                          proto=proto,
                          table=11))
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_tcp(self):
        proto = 'tcp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': proto}
        flow_call_list = [mock.call(actions=self._learn_egress_actions(proto,
                                                                   priority),
                                    dl_src=self.fake_port_1['mac_address'],
                                    nw_src=self.fake_port_1['fixed_ips'][0],
                                    priority=30,
                                    proto=proto,
                                    table=11)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_tcp_prefix(self):
        proto = 'tcp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': proto,
                'dest_ip_prefix': prefix}
        flow_call_list = [mock.call(actions=self._learn_egress_actions(proto,
                                                                   priority),
                                    dl_src=self.fake_port_1['mac_address'],
                                    nw_dst=prefix,
                                    nw_src=self.fake_port_1['fixed_ips'][0],
                                    priority=30,
                                    proto=proto,
                                    table=11)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_icmp(self):
        proto = 'icmp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        icmp_type = 10
        icmp_code = 20
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': proto,
                'port_range_min': icmp_type,
                'port_range_max': icmp_code}
        flow_call_list = [mock.call(actions=self._learn_egress_actions(proto,
                                                priority,
                                                icmp_type=icmp_type,
                                                icmp_code=icmp_code),
                                    dl_src=self.fake_port_1['mac_address'],
                                    nw_src=self.fake_port_1['fixed_ips'][0],
                                    priority=30,
                                    proto=proto,
                                    table=11)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_icmp_prefix(self):
        proto = 'icmp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        icmp_type = 10
        icmp_code = 20
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': 'icmp',
                'dest_ip_prefix': prefix,
                'port_range_min': icmp_type,
                'port_range_max': icmp_code}
        flow_call_list = [mock.call(actions=self._learn_egress_actions(proto,
                                                priority,
                                                icmp_type=icmp_type,
                                                icmp_code=icmp_code),
                                    dl_src=self.fake_port_1['mac_address'],
                                    nw_dst=prefix,
                                    nw_src=self.fake_port_1['fixed_ips'][0],
                                    priority=30,
                                    proto=proto,
                                    table=11)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_tcp_port(self):
        proto = 'tcp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': proto,
                'port_range_min': 10,
                'port_range_max': 10}
        flow_call_list = [mock.call(actions=self._learn_egress_actions(proto,
                                                priority),
                                    dl_src=self.fake_port_1['mac_address'],
                                    nw_src=self.fake_port_1['fixed_ips'][0],
                                    priority=30,
                                    proto=proto,
                                    table=11,
                                    tcp_dst=rule['port_range_min'])]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_tcp_mport(self):
        proto = 'tcp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': proto,
                'port_range_min': 10,
                'port_range_max': 100}
        tcp_dst = ['0x000a/0xfffe', '0x000c/0xfffc', '0x0010/0xfff0',
                   '0x0020/0xffe0', '0x0044/0xffe0', '0x0060/0xfffc',
                   '0x0064']
        flow_call_list = []
        for port in tcp_dst:
            flow_call_list.append(
                mock.call(actions=self._learn_egress_actions(proto,
                                                         priority),
                          dl_src=self.fake_port_1['mac_address'],
                          nw_src=self.fake_port_1['fixed_ips'][0],
                          priority=30,
                          proto=proto,
                          table=11,
                          tcp_dst=port))
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_tcp_mport_prefix(self):
        proto = 'tcp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': 'tcp',
                'port_range_min': 10,
                'port_range_max': 100,
                'dest_ip_prefix': prefix}
        tcp_dst = ['0x000a/0xfffe', '0x000c/0xfffc', '0x0010/0xfff0',
                   '0x0020/0xffe0', '0x0044/0xffe0', '0x0060/0xfffc',
                   '0x0064']
        flow_call_list = []
        for port in tcp_dst:
            flow_call_list.append(
                mock.call(actions=self._learn_egress_actions(proto,
                                                             priority),
                          dl_src=self.fake_port_1['mac_address'],
                          nw_dst=prefix,
                          nw_src=self.fake_port_1['fixed_ips'][0],
                          priority=30,
                          proto=proto,
                          table=11,
                          tcp_dst=port))
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_udp(self):
        proto = 'udp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': proto}
        flow_call_list = [mock.call(actions=self._learn_egress_actions(proto,
                                                                   priority),
                                    dl_src=self.fake_port_1['mac_address'],
                                    nw_src=self.fake_port_1['fixed_ips'][0],
                                    priority=30,
                                    proto=proto,
                                    table=11)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_udp_prefix(self):
        proto = 'udp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': proto,
                'dest_ip_prefix': prefix}
        flow_call_list = [mock.call(actions=self._learn_egress_actions(proto,
                                                                   priority),
                                    dl_src=self.fake_port_1['mac_address'],
                                    nw_dst=prefix,
                                    nw_src=self.fake_port_1['fixed_ips'][0],
                                    priority=30,
                                    proto=proto,
                                    table=11)]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_udp_port(self):
        proto = 'udp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': proto,
                'port_range_min': 10,
                'port_range_max': 10}
        flow_call_list = [mock.call(actions=self._learn_egress_actions(proto,
                                                                   priority),
                                    dl_src=self.fake_port_1['mac_address'],
                                    nw_src=self.fake_port_1['fixed_ips'][0],
                                    priority=30,
                                    proto=proto,
                                    table=11,
                                    udp_dst=rule['port_range_min'])]
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_udp_mport(self):
        proto = 'udp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': proto,
                'port_range_min': 10,
                'port_range_max': 100}
        udp_dst = ['0x000a/0xfffe', '0x000c/0xfffc', '0x0010/0xfff0',
                   '0x0020/0xffe0', '0x0044/0xffe0', '0x0060/0xfffc',
                   '0x0064']
        flow_call_list = []
        for port in udp_dst:
            flow_call_list.append(
                mock.call(actions=self._learn_egress_actions(proto,
                                                             priority),
                          dl_src=self.fake_port_1['mac_address'],
                          nw_src=self.fake_port_1['fixed_ips'][0],
                          priority=30,
                          proto=proto,
                          table=11,
                          udp_dst=port))
        self._test_rules([rule], FAKE_SGID, flow_call_list)

    def test_filter_ipv4_egress_udp_mport_prefix(self):
        proto = 'udp'
        priority = PROTOCOLS_LEARN_ACTION_PRIO[proto]
        prefix = FAKE_PREFIX[constants.IPv4]
        rule = {'ethertype': constants.IPv4,
                'direction': 'egress',
                'protocol': proto,
                'port_range_min': 10,
                'port_range_max': 100,
                'dest_ip_prefix': prefix}
        udp_dst = ['0x000a/0xfffe', '0x000c/0xfffc', '0x0010/0xfff0',
                   '0x0020/0xffe0', '0x0044/0xffe0', '0x0060/0xfffc',
                   '0x0064']
        flow_call_list = []
        for port in udp_dst:
            flow_call_list.append(
                mock.call(actions=self._learn_egress_actions(proto,
                                                         priority),
                          dl_src=self.fake_port_1['mac_address'],
                          nw_dst=prefix,
                          nw_src=self.fake_port_1['fixed_ips'][0],
                          priority=30,
                          proto=proto,
                          table=11,
                          udp_dst=port))
        self._test_rules([rule], FAKE_SGID, flow_call_list)
