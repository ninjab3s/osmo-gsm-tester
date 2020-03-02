# osmo_gsm_tester: specifics for running an SRS EPC process
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

from . import log, util, config, template, process, remote

def rlc_drb_mode2qci(rlc_drb_mode):
    if rlc_drb_mode.upper() == "UM":
        return 7;
    elif rlc_drb_mode.upper() == "AM":
        return 9;
    raise log.Error('Unexpected rlc_drb_mode', rlc_drb_mode=rlc_drb_mode)

class srsEPC(log.Origin):

    REMOTE_DIR = '/osmo-gsm-tester-srsepc'
    BINFILE = 'srsepc'
    CFGFILE = 'srsepc.conf'
    DBFILE = 'srsepc_user_db.csv'
    PCAPFILE = 'srsepc.pcap'
    LOGFILE = 'srsepc.log'

    def __init__(self, suite_run, run_node):
        super().__init__(log.C_RUN, 'srsepc')
        self._addr = run_node.run_addr()
        self.set_name('srsepc_%s' % self._addr)
        self.run_dir = None
        self.config_file = None
        self.db_file = None
        self.log_file = None
        self.pcap_file = None
        self.process = None
        self.rem_host = None
        self.remote_config_file = None
        self.remote_db_file = None
        self.remote_log_file = None
        self.remote_pcap_file = None
        self.subscriber_list = []
        self.suite_run = suite_run
        self._run_node = run_node

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
        try:
            self.rem_host.scpfrom('scp-back-pcap', self.remote_pcap_file, self.pcap_file)
        except Exception as e:
            self.log(repr(e))

    def start(self):
        self.log('Starting srsepc')
        self.run_dir = util.Dir(self.suite_run.get_test_run_dir().new_dir(self.name()))
        self.configure()
        if self._run_node.is_local():
            self.start_locally()
        else:
            self.start_remotely()

    def start_remotely(self):
        self.inst = util.Dir(os.path.abspath(self.suite_run.trial.get_inst('srslte')))
        lib = self.inst.child('lib')
        if not os.path.isdir(lib):
            raise log.Error('No lib/ in', self.inst)
        if not self.inst.isfile('bin', srsEPC.BINFILE):
            raise log.Error('No %s binary in' % srsEPC.BINFILE, self.inst)

        self.rem_host = remote.RemoteHost(self.run_dir, self._run_node.ssh_user(), self._run_node.ssh_addr())
        remote_prefix_dir = util.Dir(srsEPC.REMOTE_DIR)
        remote_inst = util.Dir(remote_prefix_dir.child(os.path.basename(str(self.inst))))
        remote_run_dir = util.Dir(remote_prefix_dir.child(srsEPC.BINFILE))
        self.remote_config_file = remote_run_dir.child(srsEPC.CFGFILE)
        self.remote_db_file = remote_run_dir.child(srsEPC.DBFILE)
        self.remote_log_file = remote_run_dir.child(srsEPC.LOGFILE)
        self.remote_pcap_file = remote_run_dir.child(srsEPC.PCAPFILE)

        self.rem_host.recreate_remote_dir(remote_inst)
        self.rem_host.scp('scp-inst-to-remote', str(self.inst), remote_prefix_dir)
        self.rem_host.create_remote_dir(remote_run_dir)
        self.rem_host.scp('scp-cfg-to-remote', self.config_file, self.remote_config_file)
        self.rem_host.scp('scp-db-to-remote', self.db_file, self.remote_db_file)

        remote_lib = remote_inst.child('lib')
        remote_binary = remote_inst.child('bin', srsEPC.BINFILE)
        # setting capabilities will later disable use of LD_LIBRARY_PATH from ELF loader -> modify RPATH instead.
        self.log('Setting RPATH for srsepc')
        self.rem_host.change_elf_rpath(remote_binary, remote_lib)
        # srsepc requires CAP_NET_ADMIN to create tunnel devices: ioctl(TUNSETIFF):
        self.log('Applying CAP_NET_ADMIN capability to srsepc')
        self.rem_host.setcap_net_admin(remote_binary)

        args = (remote_binary, self.remote_config_file,
                '--hss.db_file=' + self.remote_db_file,
                '--log.filename=' + self.remote_log_file,
                '--pcap.enable=true',
                '--pcap.filename=' + self.remote_pcap_file)

        self.process = self.rem_host.RemoteProcess(srsEPC.BINFILE, args)
        #self.process = self.rem_host.RemoteProcessFixIgnoreSIGHUP(srsEPC.BINFILE, remote_run_dir, args)
        self.suite_run.remember_to_stop(self.process)
        self.process.launch()

    def start_locally(self):
        inst = util.Dir(os.path.abspath(self.suite_run.trial.get_inst('srslte')))

        binary = inst.child('bin', BINFILE)
        if not os.path.isfile(binary):
            raise log.Error('Binary missing:', binary)
        lib = inst.child('lib')
        if not os.path.isdir(lib):
            raise log.Error('No lib/ in', inst)

        env = {}
        # setting capabilities will later disable use of LD_LIBRARY_PATH from ELF loader -> modify RPATH instead.
        self.log('Setting RPATH for srsepc')
        # srsepc binary needs patchelf <= 0.9 (0.10 and current master fail) to avoid failing during patch. OS#4389, patchelf-GH#192.
        util.change_elf_rpath(binary, util.prepend_library_path(lib), self.run_dir.new_dir('patchelf'))
        # srsepc requires CAP_NET_ADMIN to create tunnel devices: ioctl(TUNSETIFF):
        self.log('Applying CAP_NET_ADMIN capability to srsepc')
        util.setcap_net_admin(binary, self.run_dir.new_dir('setcap_net_admin'))

        self.dbg(run_dir=self.run_dir, binary=binary, env=env)
        args = (binary, os.path.abspath(self.config_file),
                '--hss.db_file=' + self.db_file,
                '--log.filename=' + self.log_file,
                '--pcap.enable=true',
                '--pcap.filename=' + self.pcap_file)

        self.process = process.Process(self.name(), self.run_dir, args, env=env)
        self.suite_run.remember_to_stop(self.process)
        self.process.launch()

    def configure(self):
        self.config_file = self.run_dir.new_file(srsEPC.CFGFILE)
        self.db_file = self.run_dir.new_file(srsEPC.DBFILE)
        self.log_file = self.run_dir.new_file(srsEPC.LOGFILE)
        self.pcap_file = self.run_dir.new_file(srsEPC.PCAPFILE)
        self.dbg(config_file=self.config_file, db_file=self.db_file)

        values = dict(epc=config.get_defaults('srsepc'))
        config.overlay(values, self.suite_run.config())
        config.overlay(values, dict(epc={'run_addr': self.addr()}))

        # Set qci for each subscriber:
        rlc_drb_mode = values['epc'].get('rlc_drb_mode', None)
        assert rlc_drb_mode is not None
        for i in range(len(self.subscriber_list)):
            self.subscriber_list[i]['qci'] = rlc_drb_mode2qci(rlc_drb_mode)
        config.overlay(values, dict(epc=dict(hss=dict(subscribers=self.subscriber_list))))

        self.dbg('SRSEPC CONFIG:\n' + pprint.pformat(values))

        with open(self.config_file, 'w') as f:
            r = template.render(srsEPC.CFGFILE, values)
            self.dbg(r)
            f.write(r)
        with open(self.db_file, 'w') as f:
            r = template.render(srsEPC.DBFILE, values)
            self.dbg(r)
            f.write(r)

    def subscriber_add(self, modem, msisdn=None, algo_str=None):
        if msisdn is None:
            msisdn = self.suite_run.resources_pool.next_msisdn(modem)
        modem.set_msisdn(msisdn)

        if algo_str is None:
            algo_str = modem.auth_algo() or util.OSMO_AUTH_ALGO_NONE

        if algo_str != util.OSMO_AUTH_ALGO_NONE and not modem.ki():
            raise log.Error("Auth algo %r selected but no KI specified" % algo_str)

        subscriber_id = len(self.subscriber_list) # list index
        self.subscriber_list.append({'id': subscriber_id, 'imsi': modem.imsi(), 'msisdn': msisdn, 'auth_algo': algo_str, 'ki': modem.ki(), 'opc': None})

        self.log('Add subscriber', msisdn=msisdn, imsi=modem.imsi(), subscriber_id=subscriber_id,
                 algo_str=algo_str)
        return subscriber_id

    def enb_is_connected(self, enb):
        # FIXME: srspec's stdout: "S1 Setup Request - eNB Id 0x66c0", but srsenb.conf has "enb_id = 0x19B"
        return 'S1 Setup Request - eNB Id' in (self.process.get_stdout() or '')

    def running(self):
        return not self.process.terminated()

    def addr(self):
        return self._addr

    def tun_addr(self):
        return '172.16.0.1'

    def run_node(self):
        return self._run_node

# vim: expandtab tabstop=4 shiftwidth=4