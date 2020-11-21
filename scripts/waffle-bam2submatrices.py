import os
from math                            import isnan

import matplotlib
matplotlib.use('Agg')

from subprocess                      import Popen
from multiprocessing                 import cpu_count
from argparse                        import ArgumentParser
from collections                     import OrderedDict
from random                          import getrandbits

from pytadbit.parsers.hic_bam_parser import get_biases_region, _iter_matrix_frags, get_matrix
from pytadbit.parsers.hic_bam_parser import read_bam, filters_to_bin, printime
from pytadbit.utils.file_handling    import mkdir

from scipy.stats.stats               import spearmanr
from pysam                           import AlignmentFile
import numpy as np

from meta_waffle.stats               import matrix_to_decay, get_center


def write_matrix(inbam, resolution, biases, outfile,
                 filter_exclude=(1, 2, 3, 4, 6, 7, 8, 9, 10),
                 region1=None, start1=None, end1=None, clean=True,
                 region2=None, start2=None, end2=None, nchunks=100,
                 tmpdir='.', ncpus=8, verbose=True,
                 square_size=1000, waffle_radii=10):

    # if not isinstance(filter_exclude, int):
    #     filter_exclude = filters_to_bin(filter_exclude)

    bamfile = AlignmentFile(inbam, 'rb')
    sections = OrderedDict(zip(bamfile.references,
                               [x // resolution + 1 for x in bamfile.lengths]))

    total = 0
    section_pos = dict()
    for crm in sections:
        section_pos[crm] = (total, total + sections[crm])
        total += sections[crm]


    # we now run a sliding square along the genomic matrix retrieve the
    # interaction matrix corresponding to the sliding square

    mkdir(os.path.split(os.path.abspath(outfile))[0])
    # write the rest of the file to be sorted
    out = open(outfile, 'w')
    nheader = 0
    for i, c in enumerate(bamfile.references):
        out.write('# CHROM\t{}\t{}\n'.format(c, bamfile.lengths[i]))
        nheader += 1
    out.write('# RESOLUTION\t{}\n'.format(resolution))
    nheader += 1

    waffle_size = waffle_radii * 2 + 1
    matrix_size = square_size + 2 * waffle_radii
    # for chrom in section_pos:
    for chrom in ['22']:
        for pos1 in range(waffle_radii, sections[chrom], square_size):
            for pos2 in range(pos1, sections[chrom], square_size):
                # retrieve a matrix a bit bigger than needed, each queried cell will 
                # need to have a given radii around
                matrix = get_matrix(
                    inbam, resolution, filter_exclude=filter_exclude, biases=biases, 
                    ncpus=ncpus, normalization='norm',
                    region1=chrom, 
                    start1=pos1 * resolution + 1,
                    end1=min(sections[chrom],  # we want to stay inside chromosome
                             pos1 + square_size + waffle_radii * 2) * resolution,
                    region2=chrom, 
                    start2=pos2 * resolution + 1,
                    end2=min(sections[chrom],  # we want to stay inside chromosome
                             pos2 + square_size + waffle_radii * 2) * resolution,
                    tmpdir=tmpdir, nchunks=nchunks, verbose=False, clean=True
                )
                # convert to numpy array for faster querying (faster than list of lists)
                num_matrix = np.asarray([[matrix.get((i, j), 0) 
                                          for j in range(matrix_size)] 
                                         for i in range(matrix_size)])
                # another numpy array with string to convert to string only once per number
                str_matrix = np.asarray([['{:.3f}'.format(matrix.get((i, j), 0)
                                                          ).rstrip('0').rstrip('.') 
                                          for j in range(matrix_size)] 
                                         for i in range(matrix_size)])

                # iterate over each cell inside the inner matrix
                # extract a waffle around each cell and do stats
                for i in range(waffle_radii, square_size + waffle_radii):
                    # we do not want anything outside chromosome
                    if pos1 + i > sections[chrom]:
                        break
                    for j in range(waffle_radii, square_size + waffle_radii):
                        # we do not want anything crossing over the diagonal
                        if pos1 + i > pos2 + j - waffle_size:
                            continue
                        # we do not want anything outside chromosome
                        if pos2 + j > sections[chrom]:
                            break
                        ## get waffle
                        waffle = num_matrix[i - waffle_radii:i + waffle_radii + 1, 
                                            j - waffle_radii:j + waffle_radii + 1]
                        # if it's all zeroes we do not want it
                        if not waffle.sum():
                            continue
                        # if it's smaller than expected we do not want it
                        if len(waffle) < waffle_size:
                            continue
                        ## stats
                        # spearman
                        x, y = matrix_to_decay(waffle, len(waffle), metric='loop')
                        rho, pval = spearmanr(x, y)
                        # if nan, the matrix is too sparse and we do not want it
                        if isnan(rho):
                            continue
                        # peak intensity
                        peak = get_center(waffle, len(waffle), span=1)
                        ## store waffle and stats
                        waffle = str_matrix[i - waffle_radii:i + waffle_radii + 1, 
                                            j - waffle_radii:j + waffle_radii + 1]
                        out.write('{}\t{}\t{}\t{}\t{}\t{}\n'.format(
                            pos1 + i + 1, pos2 + j + 1, rho, pval, peak, 
                            ','.join(v for l in waffle for v in l)))
    out.close()

    return nheader


def sort_BAMtsv(nheader, outfile, tmp):
    tsv = outfile
    printime('Sorting BAM matrix: {}'.format(tsv))
    # sort file first and second column and write to same file
    print(("(head -n {0} {1} && tail -n +{0} {1} | "
               "sort -k1n -k2n -S 10% -T {2}) > {1}").format(
                   nheader, tsv, tmp))
    _ = Popen(("(head -n {0} {2} && tail -n +{1} {2} | "
               "sort -k1n -k2n -S 10% -T {3}) > {2}_").format(
                   nheader, nheader + 1, tsv, tmp), shell=True).communicate()
    os.system("mv {0}_ {0}".format(tsv))


def main():
    opts        = get_options()
    inbam       = opts.inbam
    resolution  = opts.resolution
    outfile     = opts.outfile
    tmppath     = opts.tmppath
    biases_file = opts.biases_file

    nheader = write_matrix(inbam, resolution, biases_file, outfile, nchunks=opts.nchunks,
                           ncpus=opts.ncpus, clean=opts.clean, 
                           square_size=opts.chunk_size, waffle_radii=opts.waffle_radii)

    rand_hash = "%016x" % getrandbits(64)
    tmpdir = os.path.join(tmppath, '_tmp_%s' % (rand_hash))
    mkdir(tmpdir)

    #sort all files for only read once per pair of peaks to extract
    sort_BAMtsv(nheader, outfile, tmpdir)

    os.system('rm -rf {}'.format(tmpdir))

    printime('Done.')


def get_options():
    parser = ArgumentParser()

    parser.add_argument('-bam', '--bam', dest='inbam', required=True, default=False,
                        help='Input HiC-BAM file')
    parser.add_argument('-r', '--resolution', dest='resolution', required=True, default=False,
                        type=int, help='wanted resolution from generated matrix')
    parser.add_argument('-o', '--out', dest='outfile', required=True, default=False,
                        help='Output file to store counts')
    parser.add_argument('-b', '--biases', dest='biases_file', required=True, default=False,
                        help='Pickle file with biases')
    parser.add_argument('--tmp', dest='tmppath', required=False, default='/tmp',
                        help='[%(default)s] Path to temporary folder')
    parser.add_argument('--keep_tmp', dest='clean', default=True, action='store_false',
                        help='Keep temporary files for debugging')
    parser.add_argument('-C', dest='ncpus', default=cpu_count(),
                        type=int, help='Number of CPUs used to read BAM')
    parser.add_argument('--nchunks', dest='nchunks', default=100, metavar='INT',
                        type=int, help='''[%(default)s] chunks in which to cut
                        input bam file (in the BAM parsing step)''')
    parser.add_argument('--chunk_size', type=int, default=1000, metavar='INT',
                        help='''[%(default)s]
                        to avoid overloading memory, scans the genomic matrix 
                        in chunks of the given size (in the waffling step)''')
    parser.add_argument('--waffle_radii', type=int, default=10,  metavar='INT',
                        help='''[%(default)s]
                        number of bins around a given position to extract for
                        the waffle.''')
    opts = parser.parse_args()

    return opts


if __name__ == '__main__':
    exit(main())