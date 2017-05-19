#!/usr/bin/env python3
# -*-coding:Utf-8 -*

# Copyright (C) 2015 Laurent Modolo
 
# This file is part of TEtools suite for galaxy.

# TEtools is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# TEtools is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with TEtools.  If not, see <http://www.gnu.org/licenses/>.

import sys
if sys.version_info[0] == 2:
    print("TEcount.py is only compatible with python3. Please run TEcount.py as an executable or with the command 'python3 TEcount.py'")
    exit(0)

import argparse
import configparser
from os import path
from os import stat
from os import mkdir
from subprocess import Popen
from subprocess import PIPE
from threading import Thread
from queue import Queue, Empty
import shlex
import atexit

tools_path = path.realpath(__file__)[:-len('TEcount.py')]
config = configparser.ConfigParser()
if not path.isfile(tools_path+'TEcount.ini'):
    print("'TEcount.ini' file not found, writing default one.")
    config['programs'] = {'urqt': tools_path+'UrQt',
                          'bowtie': tools_path+'bowtie/bowtie',
                          'bowtie2': tools_path+'bowtie2/bowtie2'}
    with open(tools_path+'TEcount.ini', 'w') as configfile:
        config.write(configfile)
config.read(tools_path+'TEcount.ini')

parser = argparse.ArgumentParser(prog='TEcount.py')
parser.add_argument('-RNA', action='store', dest='fastq_files', help='RNA fastq file(s)', nargs='*')
parser.add_argument('-RNApair', action='store', dest='fastq_pair_files', help='RNA fastq file(s)', nargs='*')
parser.add_argument('-sam', action='store', dest='sam_files', help='RNA sam file(s)', nargs='*')
parser.add_argument('-insert', action='store', dest='insert_size', default=500, help='insert size for paired-end data')
parser.add_argument('-TE_fasta', action='store', dest='fasta_file', default=None, help='TE sequence fasta file')
parser.add_argument('-bowtie2', action='store_true', dest='bowtie2', default=False, help='use bowtie2 instead of bowtie')
parser.add_argument('-MAPQ', action='store', dest='mapq', default=255, help='minimal mapping quality (from 0 to 255, 0 the best)')
parser.add_argument('-QC', action='store_true', dest='quality_control', default=False, help='use UrQt to perfom quality trimming on the fastq files')
parser.add_argument('-rosette', action='store', dest='rosette_file', help='Rosette file for TE name')
parser.add_argument('-column', action='store', dest='count_column', default=2, help='Rosette column to group count')
parser.add_argument('-count', action='store', dest='count_file', default=None, help='output count file')
parser.add_argument('-siRNA', action='store', dest='count_sirna_file', default=None, help='ouptput siRNA count file: store siRNA count (21bp by default) in another file than piRNA (other size), this option is not compatible with -QC')
parser.add_argument('-sirna_size', action='store', dest='sirna_size', default=21, help='siRNA size (default 21), moved from the TEcount.ini file (UPPMAX)')
parser.add_argument('-thread', action='store', dest='thread', default=3, help='number of threads (default 3), the default will fill a single core, moved from the TEcount.ini file (UPPMAX)')
parser.add_argument('-version', action='store_true', dest='version', default=False, help='print version')
args = parser.parse_args()

if args.version:
    print("1.0.0")
    exit(0)

# moved from TEcount.ini to arguments, now add back to config[] (UPPMAX)
config['programs']['sirna_size'] = args.sirna_size
config['programs']['thread'] = args.thread

# subprosses management


def read_output(pipe, funcs):
    for line in iter(pipe.readline, b''):
        for func in funcs:
            func(line.decode('utf-8'))
    pipe.close()


def write_output(get):
    for line in iter(get, None):
        print(line[:-1])


def run_cmd(procs, command, passthrough=True):
    outs, errs = None, None
    print(command)
    procs.append(Popen(shlex.split(command), shell=False, stdout=PIPE, stderr=PIPE, bufsize=1))
    i = len(procs)-1
    if passthrough:
        outs, errs = [], []
        q = Queue()
        stdout_thread = Thread(target=read_output,
                               args=(procs[i].stdout, [q.put, outs.append]))
        stderr_thread = Thread(target=read_output,
                               args=(procs[i].stderr, [q.put, errs.append]))
        writer_thread = Thread(target=write_output,
                               args=(q.get,))
        for t in (stdout_thread, stderr_thread, writer_thread):
            t.daemon = True
            t.start()
        procs[i].wait()
        for t in (stdout_thread, stderr_thread):
            t.join()
        q.put(None)
        outs = ' '.join(outs)
        errs = ' '.join(errs)
    else:
        outs, errs = procs[i].communicate()
        outs = '' if outs == None else outs.decode('utf-8')
        errs = '' if errs == None else errs.decode('utf-8')
    rc = procs[i].returncode
    procs.pop()
    return (rc, (outs, errs))

class Double_dict:
    # structure to access item by it's value or key which store
    # highly redundent data like TE sequence sharing the same familly name
    number_of_dict = 0

    def __init__(self):
        self.forward_dict = dict()  # where we store item according to key
        self.reverse_dict = dict()  # where we store key according to item

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.forward_dict[key]
        else:
            return self.reverse_dict[key]

    def __setitem__(self, key, item):
        if isinstance(key, int):
            self.forward_dict[key] = item
        else:
            self.reverse_dict[key] = item

    def __contains__(self, item):
        if item in self.reverse_dict.keys():
            return True
        else:
            return False

    def __len__(self):
        return len(self.forward_dict)

    def keys(self):
        return self.reverse_dict.keys()

    def add(self, item):
        # when we add an item, we return it's keys
        # if the item is already present do nothing except returning it's key
        if item in self.reverse_dict.keys():
            return self.reverse_dict[item]
        else:
            key = len(self.forward_dict.keys())
            self.forward_dict[key] = item
            self.reverse_dict[item] = key
            return key


class Rosette:
    def __init__(self, rosette_file, count_column, count_file, sample_number,
                 count_sirna_file):
        if count_sirna_file is not None:
            self.count_sirna = True
            self.count_sirna_file = str(count_sirna_file)
        else:
            self.count_sirna = False
        self.sample_number = int(sample_number)
        if count_file is None:
            print("error: no count output file given")
            exit(1)
        self.count_file = str(count_file)
        self.current_sample = -1
        self.count_column = int(count_column)-1
        if self.count_column <= 0:
            self.count_column = 1
            print('wrong column number, changing column to count to the \
                  first one.')
        # the count are stored in a dictionnary which associate a variable id
        # (the variable corresponding to the column count_column)
        # to it's read mapping number
        self.TE_count_variable = dict()
        self.TE_count = dict()
        self.TE_count_total = dict()
        self.TE_count_valid = dict()
        self.purged = False

        # each TE copy has an unique identifier in the first column of the
        # rosette file
        self.TE_identifier = dict()

        # we count the number of variables in the rosette file
        self.column_number = 0
        with open(rosette_file) as rosette_file_handle:
            line = rosette_file_handle.readline()
            self.column_number = len(line.split())

        # we add a Double_dict object for each of these variable
        # the first column corresponds to the TE_identifier
        self.TE = list()
        self.TE.append(Double_dict())
        for i in range(self.column_number-1):
            self.TE.append(Double_dict())

        # we do the same thing for the siRNA if we need to count them
        if self.count_sirna:
            self.sirna_count = dict()
            self.sirna_count_total = dict()

        # we load the rosette file information into memory
        self.read(rosette_file)

    def sirna(self):
        return self.count_sirna

    @staticmethod
    def format_identifier(identifier):
        return str(identifier)

    @staticmethod
    def format_variable(variable):
        return str(variable)

    def add(self, line):
        # the first column correspond to an unique TE copy identifier
        TE_identifier = self.format_identifier(line[0])

        # list of id corresponding to the item added in the Double_dict inside
        # the self.TE list
        keys = list()
        # we add the count column first
        # print(line)
        # print(line[self.count_column])
        keys.append( self.TE[self.count_column].add(self.format_variable(line[self.count_column])) )
        if self.column_number-1 > 1:
            for i in range(self.column_number-1):
                if i+1 != self.count_column:
                    keys.append(self.TE[i].add(
                        self.format_variable(line[i+1])))
        # we associate all the keys associated with the value of the rosette
        # line to the TE identifier
        self.TE_identifier[TE_identifier] = keys
        # we do the same thing with the varaible on which we are going to count
        # the reads
        count_variable = self.format_variable(line[self.count_column])
        self.TE_count_variable[count_variable] = keys
        self.TE_count[count_variable] = [0]*self.sample_number
        self.TE_count_total[count_variable] = 0
        self.TE_count_valid[count_variable] = True

        # we do the same thing for the siRNA if we need to count them
        if self.count_sirna:
            self.sirna_count[count_variable] = [0]*self.sample_number
            self.sirna_count_total[count_variable] = 0
        # print(str(self.TE_count_variable[self.format_variable(
        # line[self.count_column])])+' '+
        # str(self.TE_identifier[TE_identifier]))

    def read(self, rosette_file):
        i = 0
        with open(rosette_file) as rosette_file_handle:
            for line in rosette_file_handle:
                i += 1
                line = line.split()
                if len(line) != self.column_number:
                    print('line '+str(i)+' has '+str(len(line))+' rows')
                self.add(line)

    def count(self, identifier, count):
        TE_identifier = self.format_identifier(identifier)
        if TE_identifier in self.TE_identifier.keys():
            keys = self.TE_identifier[TE_identifier]
            count_variable = self.TE[self.count_column][keys[0]]
            self.TE_count[count_variable][self.current_sample] += int(count)
            self.TE_count_total[count_variable] += int(count)
        else:
            print(str(identifier)+' not found in the Rosette file')

    def count_si(self, identifier, count):
        TE_identifier = self.format_identifier(identifier)
        if TE_identifier in self.TE_identifier.keys():
            keys = self.TE_identifier[TE_identifier]
            count_variable = self.TE[self.count_column][keys[0]]
            self.sirna_count[count_variable][self.current_sample] += int(count)
            self.sirna_count_total[count_variable] += int(count)
        else:
            print(str(identifier)+' not found in the Rosette file')

    def get_count_variable(self, identifier):
        TE_identifier = self.format_identifier(identifier)
        if TE_identifier in self.TE_identifier.keys():
            keys = self.TE_identifier[TE_identifier]
            return self.TE[self.count_column][int(keys[0])]
        else:
            print(str(TE_identifier)+' copy name not found \
                  in the Rosette file')
            return None

    def reset_count(self):
        self.current_sample += 1
        # we reset the counter of reads for each TE grouped by count variable
        # the total count is never reset
        if self.count_sirna:
            for item in self.TE_count_variable.keys():
                self.TE_count[item][self.current_sample] = 0
                self.sirna_count[item][self.current_sample] = 0
        else:
            for item in self.TE_count_variable.keys():
                self.TE_count[item][self.current_sample] = 0

    def purge(self, fasta_file):
        # we remove TE copy that are not present in the fasta file of
        # TE copy sequences
        for key in self.TE_count_valid.keys():
            self.TE_count_valid[key] = False
        with open(fasta_file) as fasta_file_handle:
            for line in fasta_file_handle:
                if line[0] == '>':
                    line = line.split()
                    count_variable = self.get_count_variable(line[0][1:])
                    if count_variable is not None:
                        self.TE_count_valid[count_variable] = True
        self.purged = True

    def write(self):
        if self.count_sirna:
            with open(self.count_file, "w") as output_file_handle, open(self.count_sirna_file, "w") as output_sirna_file_handle:
                for item in sorted(self.TE_count_variable, key=lambda key: self.TE_count_variable[key]):
                    if self.TE_count_valid[item]:
                        keys = self.TE_count_variable[item]
                        output_file_handle.write(str( self.TE[self.count_column][keys[0]])+' ')
                        output_sirna_file_handle.write(str( self.TE[self.count_column][keys[0]] )+' ')
                        for i in range(self.column_number-1):
                            if i+1 != self.count_column:
                                output_file_handle.write(str( self.TE[i][keys[i]] )+' ')
                                output_sirna_file_handle.write(str( self.TE[i][keys[i]] )+' ')
                        # the last column correspond to the count for the count_variable variable
                        for count in self.TE_count[item]:
                            output_file_handle.write(str(count)+' ')
                        for count in self.sirna_count[item]:
                            output_sirna_file_handle.write(str(count)+' ')
                        output_file_handle.write(str(self.TE_count_total[item]))
                        output_file_handle.write("\n")
                        output_sirna_file_handle.write(str(self.sirna_count_total[item]))
                        output_sirna_file_handle.write("\n")
        else:
            with open(self.count_file, "w") as output_file_handle:
                for item in sorted(self.TE_count_variable, key=lambda key: self.TE_count_variable[key]):
                    if self.TE_count_valid[item]:
                        keys = self.TE_count_variable[item]
                        output_file_handle.write(str( self.TE[self.count_column][keys[0]] )+' ')
                        for i in range(self.column_number-1):
                            if i+1 != self.count_column:
                                output_file_handle.write(str( self.TE[i][keys[i]] )+' ')
                        # the last column correspond to the count
                        # for the count_variable variable
                        for count in self.TE_count[item]:
                            output_file_handle.write(str(count)+' ')
                        output_file_handle.write(str(self.TE_count_total[item]))
                        output_file_handle.write("\n")


class Count:
    procs = list()
    # we want to kill every subprocess if we kill TEcount.py
    def kill_subprocesses(self):
        if len(self.procs) > 0:
            for proc in self.procs:
                proc.kill()

    def __init__(self, config, rosette, max_mapq, bowtie2=False,
                 fasta_file=None):
        self.config = config
        self.rosette = rosette
        self.max_mapq = int(max_mapq)
        self.bowtie2 = bool(bowtie2)
        self.index_build = False
        self.fasta_file = str(fasta_file)
        self.index_file = None
        self.rosette.purge(str(self.fasta_file))

    def __mapping_index(self):
        print('building index')
        if path.isfile(self.fasta_file):
            self.index_file = self.fasta_file+'.index'
            bowtie_cmd = str()
            if self.bowtie2:
                self.index_file += str(2)
                bowtie_cmd += self.config['bowtie2']+'-build'
            else:
                bowtie_cmd += self.config['bowtie']+'-build'
            bowtie_cmd += ' -f '+self.fasta_file+' '+self.index_file
            run_cmd(self.procs, bowtie_cmd)
        else:
            print(str(self.fasta_file)+' file not found')
            exit(1)
        self.index_build = True

    def from_raw_fastq(self, fastq_file, fastq_pair_file=None,
                       insert_size=None):
        self.rosette.reset_count()
        if path.isfile(fastq_file):
            self.fastq_file = str(fastq_file)
        else:
            print(str(fastq_file)+' file not found')
            exit(1)
        if fastq_pair_file is None:
            self.paired = False
            self.fastq_pair_file = str(fastq_pair_file)
        else:
            self.paired = True
            if path.isfile(fastq_pair_file):
                self.fastq_pair_file = str(fastq_pair_file)
            else:
                print(str(fastq_pair_file)+' file not found')
                exit(1)
            self.insert_size = int(insert_size)
        self.__sam_file()
        self.__quality_trimming()
        self.__mapping_index()
        self.__mapping()
        self.__count()

    def from_fastq(self, fastq_file, fastq_pair_file, insert_size):
        self.rosette.reset_count()
        if path.isfile(fastq_file):
            self.fastq_file = str(fastq_file)
        else:
            print(str(fastq_file)+' file not found')
            exit(1)
        if fastq_pair_file is None:
            self.paired = False
            self.fastq_pair_file = str(fastq_pair_file)
        else:
            self.paired = True
            if path.isfile(fastq_pair_file):
                self.fastq_pair_file = str(fastq_pair_file)
            else:
                print(str(fastq_pair_file)+' file not found')
                exit(1)
            self.insert_size = int(insert_size)
        self.__sam_file()
        self.__mapping_index()
        self.__mapping()
        self.__count()

    def from_sam(self, sam_file):
        self.rosette.reset_count()
        if path.isfile(sam_file):
            self.sam_file = str(sam_file)
        else:
            print(str(sam_file)+' file not found')
            exit(1)
        self.__count()

    def __sam_file(self):
        working_directory = './alignment/'
        try:
            stat(working_directory)
        except:
            mkdir(working_directory)
        self.sam_file = working_directory+path.split((path.splitext(self.fastq_file)[0]+'.sam'))[1]
        print(self.sam_file)

    def __quality_trimming(self):
        print('quality trimming using UrQt')
        urqt_cmd = str(self.config['urqt'])
        urqt_cmd += ' --m '+str(self.config['thread'])+' --t 20'+' --in '+str(self.fastq_file)+' --out QC_'+str(self.fastq_file)+' --v'
        self.fastq_file = 'QC_'+self.fastq_file
        if self.paired:
            if path.isfile(self.fastq_pair_file):
                urqt_cmd += ' --inpair '+str(self.fastq_pair_file)+' --outpair QC_'+str(self.fastq_pair_file)
                self.fastq_pair_file = 'QC_'+self.fastq_pair_file
            else:
                print(str(self.fastq_pair_file)+' file not found')
                exit(1)
        run_cmd(self.procs, urqt_cmd)

    def __mapping(self):
        print('mapping reads')
        bowtie_cmd = ''
        if self.bowtie2:
            bowtie_cmd = str(self.config['bowtie2'])+' -p '+str(self.config['thread'])+' --time'+' --very-sensitive'+' -x '+str(self.index_file)
            if self.paired:
                bowtie_cmd += ' --dovetail'+' -X '+str(self.insert_size)+' -1 '+str(self.fastq_file)+' -2 '+str(self.fastq_pair_file)
            else:
                bowtie_cmd += ' -U '+str(self.fastq_file)
            bowtie_cmd += ' -S '+str(self.sam_file)
        else:
            bowtie_cmd = str(self.config['bowtie'])+' -S'+' -p '+str(self.config['thread'])+' --time'+' --chunkmbs 200'+' --best'+' '+str(self.index_file)+' '+str(self.fastq_file)+' '+str(self.sam_file)
        run_cmd(self.procs, bowtie_cmd)

    def __count(self):
        # if we want to count separatly the siRNA:
        if self.rosette.sirna():
            with open(self.sam_file) as sam_file_handle:
                for line in sam_file_handle:
                    if line[0] != '@':
                        line = line.split()
                        if len(line) > 4 and line[2][0] != '*' and int(line[4]) <= self.max_mapq:
                            if len(line[9]) != int(self.config['sirna_size']):
                                self.rosette.count(str(line[2]), 1)
                            else:
                                self.rosette.count_si(str(line[2]), 1)
        else:
            with open(self.sam_file) as sam_file_handle:
                for line in sam_file_handle:
                    if line[0] != '@':
                        line = line.split()
                        if len(line) > 4 and line[2][0] != '*' and int(line[4]) <= self.max_mapq:
                            self.rosette.count(str(line[2]), 1)

    def write(self):
        self.rosette.write()

if args.fastq_files is not None and args.sam_files is not None and len(args.sam_files) < len(args.fastq_files):
    print('the number of sam files ('+str(len(args.sam_files))+') must be equal to the number of fastq_files ('+str(len(args.fastq_files))+')')
    exit(1)

sample_number = 0
if args.fastq_files is not None:
    sample_number = len(args.fastq_files)
if args.sam_files is not None:
    sample_number = len(args.sam_files)
if sample_number == 0:
    print('error: need at least one sample to count something')
    exit(1)


@atexit.register
def kill_subprocesses():
    count.kill_subprocesses()

print("loading rosette file...")
rosette = Rosette(args.rosette_file, args.count_column, args.count_file,
                  sample_number, args.count_sirna_file)
print("loading rosette fasta file...")
count = Count(config['programs'], rosette, args.mapq, args.bowtie2,
              args.fasta_file)
for i in range(sample_number):
    if args.sam_files is not None and path.isfile(args.sam_files[i]):
        print("counting " + str(args.sam_files[i]) + "...")
        count.from_sam(args.sam_files[i])
    else:
        paired_file = None
        print("counting " + str(args.fastq_files[i]) + "...")
        if args.fastq_pair_files is not None and len(args.fastq_files) == len(args.fastq_pair_files):
            paired_file = args.fastq_pair_files[i]
            print("counting " + str(args.fastq_pair_files[i]) + "...")
        if args.quality_control and not rosette.sirna():
            count.from_raw_fastq(args.fastq_files[i], paired_file,
                                 args.insert_size)
        else:
            count.from_fastq(args.fastq_files[i], paired_file,
                             args.insert_size)
count.write()
