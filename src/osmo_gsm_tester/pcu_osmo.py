# osmo_gsm_tester: specifics for running an osmo-pcu
#
# Copyright (C) 2016-2017 by sysmocom - s.f.m.c. GmbH
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
import tempfile
from . import log, config, util, template, process, event_loop

class OsmoPcu(log.Origin):
    suite_run = None
    run_dir = None
    inst = None
    env = None

    BIN_PCU = 'osmo-pcu'
    PCU_OSMO_CFG = 'osmo-pcu.cfg'

    def __init__(self, suite_run, bts, conf):
        super().__init__(log.C_RUN, OsmoPcu.BIN_PCU)
        self.suite_run = suite_run
        self.bts = bts
        self.conf = conf
        self.env = {}

    def start(self):
        self.run_dir = util.Dir(self.suite_run.get_test_run_dir().new_dir(self.name()))
        self.configure()

        self.inst = util.Dir(os.path.abspath(self.suite_run.trial.get_inst('osmo-pcu')))
        lib = self.inst.child('lib')
        if not os.path.isdir(lib):
            raise RuntimeError('No lib/ in %r' % self.inst)
        self.env = { 'LD_LIBRARY_PATH': util.prepend_library_path(lib) }

        self.launch_process(OsmoPcu.BIN_PCU, '-r', '1',
                            '-c', os.path.abspath(self.config_file),
                            '-i', self.bts.bsc.addr())
        self.suite_run.poll()

    def launch_process(self, binary_name, *args):
        binary = os.path.abspath(self.inst.child('bin', binary_name))
        run_dir = self.run_dir.new_dir(binary_name)
        if not os.path.isfile(binary):
            raise RuntimeError('Binary missing: %r' % binary)
        proc = process.Process(binary_name, run_dir,
                               (binary,) + args,
                               env=self.env)
        self.suite_run.remember_to_stop(proc)
        proc.launch()
        return proc

    def configure(self):
        self.config_file = self.run_dir.new_file(OsmoPcu.PCU_OSMO_CFG)
        self.dbg(config_file=self.config_file)

        values = dict(osmo_pcu=config.get_defaults('osmo_pcu'))
        config.overlay(values, self.suite_run.config())
        config.overlay(values, {
                        'osmo_pcu': {
                            'pcu_socket_path': self.bts.pcu_socket_path(),
                        }
        })
        config.overlay(values, { 'osmo_pcu': self.conf })

        self.dbg('OSMO-PCU CONFIG:\n' + pprint.pformat(values))

        with open(self.config_file, 'w') as f:
            r = template.render(OsmoPcu.PCU_OSMO_CFG, values)
            self.dbg(r)
            f.write(r)

# vim: expandtab tabstop=4 shiftwidth=4