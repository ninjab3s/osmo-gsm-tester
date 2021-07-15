# osmo_gsm_tester: specifics for running an SRS UE process
#
# Copyright (C) 2020 by sysmocom - s.f.m.c. GmbH
#
# Author: Pau Espin Pedrol <pespin@sysmocom.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import pprint

from ..core import log, util, config, template, process, remote
from ..core import schema
from ..core.event_loop import MainLoop
from .run_node import RunNode
from .ms import MS

def on_register_schemas():
    resource_schema = {
        'use_custom_band255': schema.BOOL_STR,
        'custom_band_list[].number': schema.UINT,
        'custom_band_list[].dl_earfcn_min': schema.UINT,
        'custom_band_list[].dl_earfcn_max': schema.UINT,
        'custom_band_list[].dl_freq_min': schema.UINT,
        'custom_band_list[].ul_freq_min': schema.UINT,
        'custom_band_list[].ul_earfcn_min': schema.UINT,
        'custom_band_list[].ul_earfcn_max': schema.UINT,        
        }
    for key, val in RunNode.schema().items():
        resource_schema['run_node.%s' % key] = val
    schema.register_resource_schema('modem', resource_schema)
    config_schema = {
        'license_server_addr': schema.IPV4,
        }
    schema.register_config_schema('amarisoft', config_schema)

def rf_type_valid(rf_type_str):
    return rf_type_str in ('uhd', 'zmq')

#reference: srsLTE.git srslte_symbol_sz()
def num_prb2symbol_sz(num_prb):
    if num_prb <= 6:
        return 128
    if num_prb <= 15:
        return 256
    if num_prb <= 50:
        return 768
    if num_prb <= 75:
        return 1024
    if num_prb <= 110:
        return 1536
    raise log.Error('invalid num_prb %r', num_prb)

def num_prb2base_srate(num_prb):
    return num_prb2symbol_sz(num_prb) * 15 * 1000

def num_prb2bandwidth(num_prb):
    if num_prb <= 6:
        return 1.4
    if num_prb <= 15:
        return 3
    if num_prb <= 25:
        return 5
    if num_prb <= 50:
        return 10
    if num_prb <= 75:
        return 15
    if num_prb <= 110:
        return 20
    raise log.Error('invalid num_prb %r', num_prb)

class AmarisoftUE(MS):

    REMOTE_DIR = '/osmo-gsm-tester-amarisoftue'
    BINFILE = 'lteue'
    CFGFILE = 'amarisoft_lteue.cfg'
    CFGFILE_RF = 'amarisoft_rf_driver.cfg'
    LOGFILE = 'lteue.log'
    IFUPFILE = 'ue-ifup'

    def __init__(self, testenv, conf):
        self._run_node = RunNode.from_conf(conf.get('run_node', {}))
        super().__init__('amarisoftue_%s' % self.addr(), testenv, conf)
        self.enb = None
        self.run_dir = None
        self.inst = None
        self._bin_prefix = None
        self.config_file = None
        self.config_rf_file = None
        self.ifup_file = None
        self.log_file = None
        self.process = None
        self.rem_host = None
        self.remote_inst = None
        self.remote_config_file = None
        self.remote_config_rf_file =  None
        self.remote_log_file = None
        self.remote_ifup_file = None
        self.num_carriers = 1
        if not rf_type_valid(conf.get('rf_dev_type', None)):
            raise log.Error('Invalid rf_dev_type=%s' % conf.get('rf_dev_type', None))
        if conf.get('rf_dev_type') == 'zmq':
            # Define all 4 possible local RF ports (2x CA with 2x2 MIMO)
            self._zmq_base_bind_port = self.testenv.suite().resource_pool().next_zmq_port_range(self, 4)

    def bin_prefix(self):
        if self._bin_prefix is None:
            self._bin_prefix = os.getenv('AMARISOFT_PATH_UE', None)
            if self._bin_prefix == None:
                self._bin_prefix  = self.testenv.suite().trial().get_inst('amarisoftue', self._run_node.run_label())
        return self._bin_prefix

    def cleanup(self):
        if self.process is None:
            return
        if self._run_node.is_local():
            return
        # copy back files (may not exist, for instance if there was an early error of process):
        try:
            self.rem_host.scpfrom('scp-back-log', self.remote_log_file, self.log_file)
        except Exception as e:
            self.log(repr(e))

    def netns(self):
        return "amarisoftue1"

    def zmq_base_bind_port(self):
        return self._zmq_base_bind_port

    def stop(self):
        self.testenv.stop_process(self.process)

    def detach(self, ue_id):
        import json
        from websocket import create_connection
        addr = self.addr()                                               # does this work?!
        port = [9002]
        ws = create_connection("ws://%s:%s" % (addr, port))

        msg = { "message": "power_off", "ue_id": int(ue_id) }
        msg_str = json.dumps(msg)
        try:
            self.dbg('sending CTRL msg: "%s"' % msg_str)
            self.ws.send(msg_str)
            self.dbg('waiting CTRL recv...')
            result = self.ws.recv()
            self.dbg('Received CTRL msg: "%s"' % result)
        except Exception:
            log.Error('Error sending CTLR msg to eNB. eNB still running?')
            pass

    def connect(self, enb):
        self.log('Starting amarisoftue')
        self.enb = enb
        self.run_dir = util.Dir(self.testenv.test().get_run_dir().new_dir(self.name()))
        self.configure()
        if self._run_node.is_local():
            self.start_locally()
        else:
            self.start_remotely()

        # send t+Enter to enable console trace
        self.dbg('Enabling console trace')
        self.process.stdin_write('t\n')

    def start_remotely(self):
        remote_binary = self.remote_inst.child('', AmarisoftUE.BINFILE)
        # setting capabilities will later disable use of LD_LIBRARY_PATH from ELF loader -> modify RPATH instead.
        self.log('Setting RPATH for ltetue')
        # patchelf >= 0.10 is required to support passing several files at once:
        self.rem_host.change_elf_rpath(remote_binary, str(self.remote_inst))
        # We also need to patch the arch-optimized binaries that lteue() will exec() into:
        self.rem_host.change_elf_rpath(self.remote_inst.child('', 'lteue-*'), str(self.remote_inst))

        # lteue requires CAP_NET_ADMIN to create tunnel devices: ioctl(TUNSETIFF):
        self.log('Applying CAP_NET_ADMIN capability to ltetue')
        self.rem_host.setcap_net_admin(remote_binary)
        self.rem_host.setcap_net_admin(self.remote_inst.child('', 'lteue-*'))

        args = (remote_binary, self.remote_config_file)
        self.process = self.rem_host.RemoteProcess(AmarisoftUE.BINFILE, args)
        self.testenv.remember_to_stop(self.process)
        self.process.launch()

    def start_locally(self):
        binary = self.inst.child('', AmarisoftUE.BINFILE)
        env = {}

        # setting capabilities will later disable use of LD_LIBRARY_PATH from ELF loader -> modify RPATH instead.
        self.log('Setting RPATH for lteue')
        util.change_elf_rpath(binary, util.prepend_library_path(self.inst), self.run_dir.new_dir('patchelf'))
        # We also need to patch the arch-optimized binaries that lteue() will exec() into:
        util.change_elf_rpath(self.inst.child('', 'lteue-*'), util.prepend_library_path(self.inst), self.run_dir.new_dir('patchelf2'))

        # lteue requires CAP_NET_ADMIN to create tunnel devices: ioctl(TUNSETIFF):
        self.log('Applying CAP_NET_ADMIN capability to lteue')
        util.setcap_net_admin(binary, self.run_dir.new_dir('setcap_net_admin'))
        util.setcap_net_admin(self.inst.child('', 'lteue-*'), self.run_dir.new_dir('setcap_net_admin2'))

        args = (binary, os.path.abspath(self.config_file))
        self.dbg(run_dir=self.run_dir, binary=binary, env=env)
        self.process = process.Process(self.name(), self.run_dir, args, env=env)
        self.testenv.remember_to_stop(self.process)
        self.process.launch()

    def gen_conf_file(self, path, filename, values):
        self.dbg('AmarisoftUE ' + filename + ':\n' + pprint.pformat(values))
        with open(path, 'w') as f:
            r = template.render(filename, values)
            self.dbg(r)
            f.write(r)

    def configure(self):
        self.inst = util.Dir(os.path.abspath(self.bin_prefix()))
        if not self.inst.isfile('', AmarisoftUE.BINFILE):
            raise log.Error('No %s binary in' % AmarisoftUE.BINFILE, self.inst)

        self.config_file = self.run_dir.child(AmarisoftUE.CFGFILE)
        self.config_rf_file = self.run_dir.child(AmarisoftUE.CFGFILE_RF)
        self.log_file = self.run_dir.child(AmarisoftUE.LOGFILE)
        self.ifup_file = self.run_dir.new_file(AmarisoftUE.IFUPFILE)
        os.chmod(self.ifup_file, 0o744) # add execution permission
        with open(self.ifup_file, 'w') as f:
            r = '''#!/bin/sh
            set -x -e
            ue_id="$1"           # UE ID
            pdn_id="$2"          # PDN unique id (start from 0)
            ifname="$3"          # Interface name
            ipv4_addr="$4"       # IPv4 address
            ipv4_dns="$5"        # IPv4 DNS
            ipv6_local_addr="$6" # IPv6 local address
            ipv6_dns="$7"        # IPv6 DNS
            old_link_local=""
            # script + sudoers file available in osmo-gsm-tester.git/utils/{bin,sudoers.d}
            sudo /usr/local/bin/osmo-gsm-tester_netns_setup.sh "%s" "$ifname" "$ipv4_addr"
            echo "${ue_id}: netns %s configured"
            ''' % (self.netns(), self.netns())
            f.write(r)

        if not self._run_node.is_local():
            self.rem_host = remote.RemoteHost(self.run_dir, self._run_node.ssh_user(), self._run_node.ssh_addr())
            remote_prefix_dir = util.Dir(AmarisoftUE.REMOTE_DIR)
            self.remote_inst = util.Dir(remote_prefix_dir.child(os.path.basename(str(self.inst))))
            remote_run_dir = util.Dir(remote_prefix_dir.child(AmarisoftUE.BINFILE))

            self.remote_config_file = remote_run_dir.child(AmarisoftUE.CFGFILE)
            self.remote_config_rf_file = remote_run_dir.child(AmarisoftUE.CFGFILE_RF)
            self.remote_log_file = remote_run_dir.child(AmarisoftUE.LOGFILE)
            self.remote_ifup_file = remote_run_dir.child(AmarisoftUE.IFUPFILE)

        values = dict(ue=config.get_defaults('amarisoft'))
        config.overlay(values, dict(ue=config.get_defaults('amarisoftue')))
        config.overlay(values, dict(ue=self.testenv.suite().config().get('amarisoft', {})))
        config.overlay(values, dict(ue=self.testenv.suite().config().get('modem', {})))
        config.overlay(values, dict(ue=self._conf))
        config.overlay(values, dict(ue=dict(addr = self.addr(),
                                            num_antennas = self.enb.num_ports(),
                                            opc = self.opc())))

        logfile = self.log_file if self._run_node.is_local() else self.remote_log_file
        ifupfile = self.ifup_file if self._run_node.is_local() else self.remote_ifup_file
        config.overlay(values, dict(ue=dict(log_filename=logfile,
                                            ifup_filename=ifupfile)))

        # Convert to Python bool and overlay config
        config.overlay(values, dict(ue={'use_custom_band255': util.str2bool(values['ue'].get('use_custom_band255', 'false'))}))

        # We need to set some specific variables programatically here to match IP addresses:
        if self._conf.get('rf_dev_type') == 'zmq':
            base_srate = num_prb2base_srate(self.enb.num_prb())
            rf_dev_args = self.enb.get_zmq_rf_dev_args_for_ue(self)

            # Single carrier
            if self.enb.num_ports() == 1:
                # SISO
                rf_dev_args += ',rx_freq0=2630e6,tx_freq0=2510e6'
            elif self.enb.num_ports() == 2:
                # MIMO
                rf_dev_args += ',rx_freq0=2630e6,rx_freq1=2630e6,tx_freq0=2510e6,tx_freq1=2510e6'

            rf_dev_args += ',id=ue,base_srate='+ str(base_srate)
            config.overlay(values, dict(ue=dict(sample_rate = base_srate / (1000*1000),
                                                rf_dev_args = rf_dev_args)))

        # The UHD rf driver seems to require the bandwidth configuration
        if self._conf.get('rf_dev_type') == 'uhd':
            bandwidth = num_prb2bandwidth(self.enb.num_prb())
            config.overlay(values, dict(ue=dict(bandwidth = bandwidth)))

        # Set UHD frame size as a function of the cell bandwidth on B2XX
        if self._conf.get('rf_dev_type') == 'uhd' and values['ue'].get('rf_dev_args', None) is not None:
            if 'b200' in values['ue'].get('rf_dev_args'):
                rf_dev_args = values['ue'].get('rf_dev_args', '')
                rf_dev_args += ',' if rf_dev_args != '' and not rf_dev_args.endswith(',') else ''

                if self.enb.num_prb() < 25:
                    rf_dev_args += 'send_frame_size=512,recv_frame_size=512'
                elif self.enb.num_prb() == 25:
                    rf_dev_args += 'send_frame_size=1024,recv_frame_size=1024'
                elif self.enb.num_prb() > 50:
                    rf_dev_args += 'num_recv_frames=64,num_send_frames=64'

                # For 15 and 20 MHz, further reduce over the wire format to sc12
                if self.enb.num_prb() >= 75:
                    rf_dev_args += ',otw_format=sc12'

                config.overlay(values, dict(ue=dict(rf_dev_args=rf_dev_args)))

        # rf driver is shared between amarisoft enb and ue, so it has a
        # different cfg namespace 'trx'. Copy needed values over there:
        config.overlay(values, dict(trx=dict(rf_dev_type=values['ue'].get('rf_dev_type', None),
                                             rf_dev_args=values['ue'].get('rf_dev_args', None),
                                             rf_dev_sync=values['ue'].get('rf_dev_sync', None),
                                             rx_gain=values['ue'].get('rx_gain', None),
                                             tx_gain=values['ue'].get('tx_gain', None),
                                            )))

        self.gen_conf_file(self.config_file, AmarisoftUE.CFGFILE, values)
        self.gen_conf_file(self.config_rf_file, AmarisoftUE.CFGFILE_RF, values)

        if not self._run_node.is_local():
            self.rem_host.recreate_remote_dir(self.remote_inst)
            self.rem_host.scp('scp-inst-to-remote', str(self.inst), remote_prefix_dir)
            self.rem_host.recreate_remote_dir(remote_run_dir)
            self.rem_host.scp('scp-cfg-to-remote', self.config_file, self.remote_config_file)
            self.rem_host.scp('scp-cfg-rf-to-remote', self.config_rf_file, self.remote_config_rf_file)
            self.rem_host.scp('scp-ifup-to-remote', self.ifup_file, self.remote_ifup_file)

    def is_registered(self, mcc_mnc=None):
        # lteue doesn't call the ifup script until after it becomes attached, so
        # simply look for our ifup script output at the end of it:
        return 'netns %s configured' % (self.netns()) in (self.process.get_stdout() or '')

    def is_rrc_connected(self):
        return self.is_registered()

    def is_attached(self):
        return self.is_registered()

    def get_assigned_addr(self, ipv6=False):
        raise log.Error('API not implemented!')

    def running(self):
        return not self.process.terminated()

    def addr(self):
        return self._run_node.run_addr()

    def run_node(self):
        return self._run_node

    def run_netns_wait(self, name, popen_args):
        if self._run_node.is_local():
            proc = process.NetNSProcess(name, self.run_dir.new_dir(name), self.netns(), popen_args, env={})
        else:
            proc = self.rem_host.RemoteNetNSProcess(name, self.netns(), popen_args, env={})
        proc.launch_sync()
        return proc

    def verify_metric(self, value, operation='avg', metric='dl_brate', criterion='gt', window=None):
        return 'metrics not yet implemented with Amarisoft UE'

# vim: expandtab tabstop=4 shiftwidth=4
