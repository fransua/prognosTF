"""
"""

from collections                     import OrderedDict
from subprocess                      import Popen
from math                            import isnan
import os

import numpy as np
from scipy.stats.stats               import pearsonr, rankdata
from pysam                           import AlignmentFile
from pickle                          import load

from pytadbit.parsers.hic_bam_parser import get_matrix
from pytadbit.utils.file_handling    import mkdir

from meta_waffle.stats               import fast_matrix_to_decay_loop, fast_matrix_to_decay_noloop, get_center, matrix_to_decay, pre_matrix_to_decay


def write_big_submatrix(matrix, chrom, pos1, pos2, 
                    sections, section_pos, out, matrix_size,
                    waffle_size, waffle_radii, square_size, metric='loop'):
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
    tpos1 = pos1 + section_pos[chrom][0]
    tpos2 = pos2 + section_pos[chrom][0]
    
    if metric == 'loop':
        fast_matrix_to_decay = fast_matrix_to_decay_loop
    else:
        fast_matrix_to_decay = fast_matrix_to_decay_noloop
    
    between_indexes  = [j + i * waffle_size 
                        for i in range(waffle_radii, waffle_size) 
                        for j in range(waffle_radii + 1)]
    outside_indexes  = [j + i * waffle_size 
                        for i in range(waffle_radii, waffle_size) 
                        for j in range(waffle_radii + 1, waffle_size)]
    outside_indexes += [j + i * waffle_size 
                        for i in range(waffle_radii) 
                        for j in range(waffle_size)]
    dist_from_center = rankdata(pre_matrix_to_decay(waffle_size))
    for i in range(waffle_radii, square_size + waffle_radii):
        # we do not want anything outside chromosome
        if pos1 + i > sections[chrom]:
            break
        for j in range(waffle_radii, square_size + waffle_radii):
            # we do not want anything crossing over the diagonal
            if pos1 + i > pos2 + j: # - waffle_size:
                continue
            # we do not want anything outside chromosome
            if pos2 + j > sections[chrom]:
                break
            ## get waffle (diagonal of the genomic matrix is located in the down left,
            ## i=waffle_size and j=0) 
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
            y = fast_matrix_to_decay(waffle, between_indexes, outside_indexes)
            # x, y = matrix_to_decay(waffle, waffle_size, metric=metric)
            rho, pval = pearsonr(dist_from_center, rankdata(y))  # equivalent of spearmanr
            # change this: x can be already known
            # if nan, the matrix is too sparse and we do not want it
            if isnan(rho):
                continue
            # peak intensity
            peak = get_center(waffle, len(waffle), span=1)
            ## store waffle and stats
            waffle = str_matrix[i - waffle_radii:i + waffle_radii + 1, 
                                j - waffle_radii:j + waffle_radii + 1]
            out.write('{}\t{}\t{:.3g}\t{:.3g}\t{:.3f}\t{}\n'.format(
                tpos1 + i, tpos2 + j, rho, pval, peak, 
                ','.join(v for l in waffle for v in l)))


def write_big_matrix(inbam, resolution, biases, outfile,
                 filter_exclude=(1, 2, 3, 4, 6, 7, 8, 9, 10),
                 wanted_chrom=None, wanted_pos1=None, wanted_pos2=None,
                 nchunks=100, tmpdir='.', ncpus=8, verbose=True,
                 clean=True, square_size=1000, waffle_radii=10,
                 dry_run=False, metric='loop'):

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

    if biases:
        fh = open(biases, 'rb')
        badcols = load(fh).get('badcol', {})
        fh.close()
    else:
        badcols = {}

    # we now run a sliding square along the genomic matrix retrieve the
    # interaction matrix corresponding to the sliding square
    mkdir(os.path.split(os.path.abspath(outfile))[0])
    # write the rest of the file to be sorted
    if not dry_run:
        out = open(outfile, 'w')
        nheader = 0
        for i, c in enumerate(bamfile.references):
            out.write('# CHROM\t{}\t{}\n'.format(c, bamfile.lengths[i]))
            nheader += 1
        out.write('# RESOLUTION\t{}\n'.format(resolution))
        nheader += 1
        out.write('# WAFFLE RADII\t{}\n'.format(waffle_radii))
        nheader += 1
        out.write('# BADCOLS\t{}\n'.format(','.join(map(str, badcols.keys()))))
        nheader += 1

    cmd = ('waffle-bam2submatrices.py -bam {} -r {} -o {} -b {} --tmp {} '
           '--chrom {} --pos1 {} --pos2 {} -C {} --nchunks {} --chunk_size {} '
           '--waffle_radii {} ').format(
        inbam, resolution, '{}', biases, tmpdir, '{}', '{}', '{}', ncpus, nchunks,
        square_size, waffle_radii)
    
    waffle_size = waffle_radii * 2 + 1
    matrix_size = square_size + 2 * waffle_radii
    for chrom in section_pos:
        for pos1 in range(0, sections[chrom], square_size):
            for pos2 in range(pos1, sections[chrom], square_size):
                if (wanted_chrom is not None and 
                    wanted_pos1  is not None and 
                    wanted_pos2  is not None):
                    if wanted_chrom != chrom or wanted_pos1 != pos1 or wanted_pos2 != pos2:
                        continue
                if dry_run:
                    print(cmd.format(outfile + '_{}:{}-{}.tsv'.format(chrom, pos1, pos2), 
                                     chrom, pos1, pos2))
                    continue
                # retrieve a matrix a bit bigger than needed, each queried cell will 
                # need to have a given radii around
                matrix = get_matrix(
                    inbam, resolution, filter_exclude=filter_exclude, biases=biases, 
                    ncpus=ncpus, normalization='decay' if biases else 'raw',
                    region1=chrom, 
                    start1=pos1 * resolution + 1,
                    end1=min(sections[chrom],  # we want to stay inside chromosome
                             pos1 + square_size + waffle_radii * 2) * resolution,
                    region2=chrom, 
                    start2=pos2 * resolution + 1,
                    end2=min(sections[chrom],  # we want to stay inside chromosome
                             pos2 + square_size + waffle_radii * 2) * resolution,
                    tmpdir=tmpdir, nchunks=nchunks, verbose=verbose, clean=clean
                )

                write_big_submatrix(matrix, chrom, pos1, pos2, 
                                    sections, section_pos, out, matrix_size,
                                    waffle_size, waffle_radii, square_size, metric=metric)

    if dry_run:
        exit()

    out.close()

    return nheader


def sort_BAMtsv(nheader, outfile, tmp):
    tsv = outfile
    # sort file first and second column and write to same file
    print(("(head -n {0} {1} && tail -n +{0} {1} | "
               "sort -k1n -k2n -S 10% -T {2}) > {1}").format(
                   nheader, tsv, tmp))
    _ = Popen(("(head -n {0} {2} && tail -n +{1} {2} | "
               "sort -k1n -k2n -S 10% -T {3}) > {2}_").format(
                   nheader, nheader + 1, tsv, tmp), shell=True).communicate()
    os.system("mv {0}_ {0}".format(tsv))
