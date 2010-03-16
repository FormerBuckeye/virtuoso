# Volatility
# Copyright (C) 2008 Volatile Systems
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details. 
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA 
#

"""
@author:       AAron Walters and Brendan Dolan-Gavitt
@license:      GNU General Public License 2.0 or later
@contact:      awalters@volatilesystems.com,bdolangavitt@wesleyan.edu
@organization: Volatile Systems
"""

from fixedint import *
from forensics.object2 import *
from forensics.object import *
from vutils import *
from forensics.win32.datetime import windows_to_unix_time
from time import ctime,asctime,gmtime
from struct import unpack,pack
from collections import defaultdict
from namedtuple import namedtuple
import cPickle as pickle

def ULInt32(buf):
    assert len(buf) == 4
    return UInt(unpack("<I", buf)[0])

def ULInt16(buf):
    assert len(buf) == 2
    return UInt(unpack("<H", buf)[0])

def ULInt8(buf):
    assert len(buf) == 1
    return UInt(unpack("<B", buf)[0])

class OutSpace:
    def __init__(self):
        self.scratch = defaultdict(dict)
        self.debug = False

    def write(self, addr, val, pack_char, label):
        if self.debug: print "Writing %#x to address %#x (output label %s)" % (val, addr, label)
        buf = pack("<" + pack_char, int(val))
        for (i,c) in enumerate(buf):
            self.scratch[label][int(addr+i)] = c

    def get_output(self, label):
        if label not in self.scratch:
            raise KeyError("Output label '%s' not defined." % label)

        data = {}
        start = 0
        buf = ""
        for i in sorted(self.scratch[label]):
            if not buf: start = i
            buf += self.scratch[label][i]
            if i+1 not in self.scratch[label]:
                data[start] = buf
                buf = ""
        # Assumption: the output buffer is contiguous
        assert len(data) == 1
        return data.values()[0]

class BufSpace:
    def __init__(self):
        self.bufs = {}
        self.debug = False
        self.current_label = 0

    def add(self, label, addr):
        label = "%s_%d" % (label, self.current_label)
        self.bufs[label] = {}
        self.bufs[label]['BASE'] = addr
        self.current_label += 1
        return label

    def read(self, label, addr, length):
        if self.debug: print "DEBUG: Reading %d bytes at %#x (malloc buffer %s)" % (length, addr, label)
        data = []
        space = self.bufs[label]

        try:
            for i in range(addr,addr+length):
                data.append(space[i])
        except KeyError, e:
            raise ValueError("Read error in dynamic buffer %s, address %#x" % (label, addr))
        if self.debug: print "DEBUG: Read", "".join(data).encode('hex')
        return "".join(data)

    def write(self, label, addr, val, pack_char):
        if self.debug: print "DEBUG: Writing %#x to address %#x (malloc buffer %s)" % (val, addr, label)
        space = self.bufs[label]

        buf = pack("<" + pack_char, int(val))
        for (i,c) in enumerate(buf):
            space[int(addr+i)] = c

    def get_scratch(self, scratch):
        data = {}
        start = 0
        buf = ""
        for i in sorted(scratch):
            if not isinstance(i, int): continue
            if not buf: start = i
            buf += scratch[i]
            if i+1 not in scratch:
                data[start] = buf
                buf = ""
        return data

    def dump(self):
        for label in self.bufs:
            print ">>> %s <<<" % label
            for start, buf in self.get_scratch(self.bufs[label]).items():
                print hex(start),":",buf.encode('hex')


class COWSpace:
    def __init__(self, base):
        self.base = base
        self.scratch = {}
        self.debug = False
    def read(self, addr, length):
        if self.debug: print "DEBUG: Reading %d bytes at %#x" % (length, addr)
        data = []
        for i in range(addr,addr+length):
            if i in self.scratch:
                data.append(self.scratch[i])
            else:
                b = self.base.read(i,1)
                if not b:
                    raise ValueError("Memory read error at %#x" % i)
                else:
                    data.append(b)
        if self.debug: print "DEBUG: Read", "".join(data).encode('hex')
        return "".join(data)

    def write(self, addr, val, pack_char):
        if self.debug: print "DEBUG: Writing %#x to address %#x" % (val, addr)
        buf = pack("<" + pack_char, int(val))
        for (i,c) in enumerate(buf):
            self.scratch[int(addr+i)] = c

    def get_scratch(self):
        data = {}
        start = 0
        buf = ""
        for i in sorted(self.scratch):
            if not buf: start = i
            buf += self.scratch[i]
            if i+1 not in self.scratch:
                data[start] = buf
                buf = ""
        return data

    def dump_scratch(self):
        for start, buf in self.get_scratch().items():
            print hex(start),":",buf.encode('hex')

class Goto(Exception):
    def __init__(self, label):
        self.label = label

Segment = namedtuple("Segment", "selector base limit flags")

def seg_base(e1, e2):
    return ((e1 >> 16) | ((e2 & 0xff) << 16) | (e2 & 0xff000000))

def seg_limit(e1, e2):
    limit = (e1 & 0xffff) | (e2 & 0x000f0000)
    if (e2 & DESC_G_MASK):
        limit = (limit << 12) | 0xfff
    return limit

def load_seg(mem, selector, GDT, LDT):
    selector &= 0xffff
    if (selector & 0x4):
        DT = LDT
    else:
        DT = GDT

    index = selector & ~7
    assert (index + 7) <= DT.limit
    
    ptr = DT.base + index
    
    # GDT/LDT entries are 64 bits. Load each half.
    e1 = ULInt32(mem.read(ptr,   4))
    e2 = ULInt32(mem.read(ptr+4, 4))
    base = seg_base(e1, e2)
    limit = seg_limit(e1, e2)
    flags = e2

    return Segment(selector, base, limit, flags)
    
def load_env(env_file):
    regs = {}
    # GP registers
    for i in range(2):
        current = env_file.readline().strip().split()
        for c in current:
            var,val = c.split('=')
            regs[var] = UInt(int(val, 16))
    # EIP and EFLAGS
    current = env_file.readline().strip()
    eip,efl,_ = current.split(" ", 2)
    for c in [eip,efl]:
        var,val = c.split('=')
        regs[var] = UInt(int(val, 16))
    # Segment registers
    for i in range(8):
        var,vals = env_file.readline().strip().split('=')
        var = var.strip()
        vals = [UInt(int(v.strip(),16)) for v in vals.split()]
        regs[var] = Segment(*vals)
    # GDT/IDT
    for i in range(2):
        var,vals = env_file.readline().strip().split('=')
        var = var.strip()
        vals = [UInt(int(v.strip(),16)) for v in vals.split()]
        regs[var] = Segment(None,vals[0],vals[1],None)
    # Control Registers
    current = env_file.readline().strip().split()
    for c in current:
        var,val = c.split('=')
        regs[var] = UInt(int(val, 16))
    
    # Check for the sysenter MSRs
    current = env_file.readline().strip().split()
    while current:
        if current[0].startswith('sysenter'):
            for c in current:
                var,val = c.split('=')
                regs[var] = UInt(int(val, 16))
            break
        current = env_file.readline().strip().split()

    return regs

PAE_SHIFT = 5
PAE_FLAG = 1 << PAE_SHIFT
DF_SHIFT = 10
DF_MASK = 1 << DF_SHIFT
DESC_G_MASK = 1 << 23

class newmicrodo(forensics.commands.command):

    # Declare meta information associated with this plugin
    
    meta_info = {} 
    meta_info['author'] = 'Brendan Dolan-Gavitt'
    meta_info['copyright'] = 'Copyright (c) 2007,2008 Brendan Dolan-Gavitt'
    meta_info['contact'] = 'brendandg@gatech.edu'
    meta_info['license'] = 'GNU General Public License 2.0 or later'
    meta_info['url'] = 'http://cc.gatech.edu/~brendan/'
    meta_info['os'] = 'WIN_32_XP_SP2'
    meta_info['version'] = '1.0'
        
    def parser(self):
        forensics.commands.command.parser(self)
        self.op.add_option('-m', '--micro-op',
            help='filename of translated micro-op trace to execute',
            dest='micro')
        self.op.add_option('-e', '--env',
            help='CPU environment to use (output of info registers in QEMU)',
            dest='env')
        self.op.add_option('-i', '--interp',
            help='Python function f that takes the outbuf and interprets it',
            dest='interp')
        self.op.add_option('-n', '--input',
            help='Input vars; should evaluate to a Python list',
            dest='inputs')
        self.op.add_option('-d', '--debug',
            help='enable debug output (print every block as executed)',
            action='store_true', default=False, dest='debug')

    def help(self):
        return  "execute translated QEMU micro-ops"

    def execute(self):
        theProfile = Profile()
        #(addr_space, symtab, types) = load_and_identify_image(self.op, self.opts)
        flat = FileAddressSpace(self.opts.filename)

        if not self.opts.env:
            self.op.error('Must provide a valid CPU environment (use "info registers")')
        if not self.opts.micro:
            self.op.error('We need some code to execute')

        # Load the CPU environment
        env = load_env(open(self.opts.env))
        if self.opts.debug:
            print "Starting environment:"
            print open(self.opts.env).read()
        locals().update(env)
        
        # Set up virtual memory
        if CR4 & PAE_FLAG:
            addr_space = IA32PagedMemoryPae(flat, int(CR3))
        else:
            addr_space = IA32PagedMemory(flat, int(CR3))

        # Wrap it in copy-on-write
        mem = COWSpace(addr_space)
        out = OutSpace()
        bufs = BufSpace()

        if self.opts.debug:
            mem.debug = True
            out.debug = True
            bufs.debug = True        

        # Inputs to the block of micro-ops we want to to execute
        #inputs = [ 0xdeadf000 ] # pBuf -- pointer to the output buffer
        if self.opts.inputs:
            inputs = eval(self.opts.inputs)
        else:
            inputs = []

        if self.opts.debug:
            print "Starting with inputs:"
            print "  inputs = [ %s ]" % (", ".join("%#x" % i for i in inputs))
            print

        # With real introspection, these would be actual values
        # from the env.
        if (EFL & DF_MASK) >> DF_SHIFT:
            DF = -1
        else:
            DF = 1

        # This actually runs the translated code
        code = pickle.load(open(self.opts.micro))
        label = 'START'
        import time
        t1 = time.time()
        while True:
            if self.opts.debug:
                print "Executing block %s" % (hex(label) if isinstance(label,int) else label)
                print "\n".join("  " + l for l in code[label].splitlines())
            try:
                exec(code[label])
            except Goto, g:
                if g.label == '__end__': break
                else: label = g.label
            except Exception, e:
                print ">>>> ERROR:",e,"<<<<"
                print code[label]
                import pdb
                pdb.post_mortem(sys.exc_info()[2])
        t2 = time.time()
        if self.opts.debug:
            print
            print "Time taken: %f ms" % ((t2-t1)*1000)

        print

        # TODO: multiple outputs
        data = out.get_output('out')
        print "Output:", data.encode('hex')

        print

        # Run the translation function
        if self.opts.debug:
            print "Decoding function:"
            if self.opts.interp:
                s = self.opts.interp.decode('string_escape')
                print "\n".join("  " + l for l in s.splitlines())
            else:
                print "(none)"
            print

        if self.opts.interp:
            print "Decoded data:"
            exec(self.opts.interp.decode('string_escape'))
            f(data)

        print 

        if self.opts.debug:
            print "Debug dump of scratch:"
            mem.dump_scratch()

        if self.opts.debug:
            print "Debug dump of scratch:"
            bufs.dump()
