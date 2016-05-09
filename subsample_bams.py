__author__ = 'dgaston'

import csv
import sys
import pysam
import getpass
import subprocess
import argparse
import argcomplete

from collections import defaultdict
from toil.job import Job

# Package methods
from ddb import configuration
from ddb_ngsflow import pipeline

from cassandra.cqlengine import connection
from cassandra.auth import PlainTextAuthProvider

from coveragestore import AmpliconCoverage
from coveragestore import SampleCoverage


def subsample_bam(addresses, keyspace, auth, name, seed, fraction, iteration):
    """Use samtools view to subsample an input file to the specified fraction"""

    logfile = "subsample-{}-{}-{}.log".format(name, fraction, iteration)
    subsampled_bam = "subsample-{}-{}-{}.bam".format(name, fraction, iteration)
    samcommand = "samtools view -s {seed}.{fraction} -b {input} > {output}".format(seed=seed, fraction=fraction,
                                                                                   input=sample, output=subsampled_bam)

    output = "{}.sambamba_coverage.bed".format(subsampled_bam)
    logfile = "{}.sambamba_coverage.log".format(subsampled_bam)

    command = ("{}".format(config['sambamba']['bin']),
               "depth region",
               "-L",
               "{}".format(samples[name]['regions']),
               "-t",
               "{}".format(config['sambamba']['num_cores']),
               "-T",
               "{}".format(config['coverage_threshold']),
               "-T",
               "{}".format(config['coverage_threshold2']),
               "{}".format(subsampled_bam),
               ">",
               "{}".format(output))

    job.fileStore.logToMaster("Samtools Command: {}\n".format(samcommand))
    pipeline.run_and_log_command(" ".join(samcommand), logfile)

    job.fileStore.logToMaster("SamBamba Coverage Command: {}\n".format(command))
    pipeline.run_and_log_command(" ".join(command), logfile)

    connection.setup(addresses, keyspace, auth_provider=auth)

    job.fileStore.logToMaster("Adding coverage data: {}\n".format(samcommand))
    with open("{}.sambamba_coverage.bed".format(sample), 'rb') as coverage:
        reader = csv.reader(coverage, delimiter='\t')
        header = reader.next()
        threshold_indices = list()
        thresholds = list()
        index = 0
        for element in header:
            if element.startswith("percentage"):
                threshold = element.replace('percentage', '')
                threshold_indices.append(index)
                thresholds.append(int(threshold))
            index += 1

        for row in reader:
            threshold_data = defaultdict(float)
            index = 0
            for threshold in thresholds:
                threshold_data[threshold] = row[threshold_indices[index]]
                index += 1

            sample_data = SampleCoverage.create(sample=sample,
                                                library_name=samples[sample]['library_name'],
                                                run_id="subsample",
                                                num_libraries_in_run=samples[sample]['num_libraries_in_run'],
                                                sequencer_id=samples[sample]['sequencer'],
                                                program_name="sambamba",
                                                extraction=samples[sample]['extraction'],
                                                panel=samples[sample]['panel'],
                                                target_pool=samples[sample]['target_pool'],
                                                amplicon=row[3],
                                                num_reads=row[4],
                                                mean_coverage=row[5],
                                                thresholds=thresholds,
                                                perc_bp_cov_at_thresholds=threshold_data)

            amplicon_data = AmpliconCoverage.create(amplicon=row[3],
                                                    sample=sample,
                                                    library_name=samples[sample]['library_name'],
                                                    run_id="subsample",
                                                    num_libraries_in_run=samples[sample]['num_libraries_in_run'],
                                                    sequencer_id=samples[sample]['sequencer'],
                                                    program_name="sambamba",
                                                    extraction=samples[sample]['extraction'],
                                                    panel=samples[sample]['panel'],
                                                    target_pool=samples[sample]['target_pool'],
                                                    num_reads=row[4],
                                                    mean_coverage=row[5],
                                                    thresholds=thresholds,
                                                    perc_bp_cov_at_thresholds=threshold_data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-e', '--seed', help="Seed number for reproducible sub-sampling")
    parser.add_argument('-n', '--number', help="Number of iterations per sample to perform", default=1)
    parser.add_argument('-s', '--samples_file', help="Input configuration file for samples")
    parser.add_argument('-c', '--configuration', help="Configuration file for various settings")

    argcomplete.autocomplete(parser)
    Job.Runner.addToilOptions(parser)
    args = parser.parse_args()
    args.logLevel = "INFO"

    samples = list()
    fractions = [50, 25]
    instructions = list()

    sys.stdout.write("Parsing configuration data\n")
    config = configuration.configure_runtime(args.configuration)

    sys.stdout.write("Parsing sample data\n")
    samples = configuration.configure_samples(args.samples_file, config)

    # Workflow Graph definition. The following workflow definition should create a valid Directed Acyclic Graph (DAG)
    root_job = Job.wrapJobFn(pipeline.spawn_batch_jobs, cores=1)

    if args.username:
        password = getpass.getpass()
        auth_provider = PlainTextAuthProvider(username=args.username, password=password)
    else:
        auth_provider = None

    with open(args.infile, 'rb') as infile:
        reader = csv.reader(infile, dialect='excel-tab')
        reader.next()
        for row in reader:
            samples.append(row[0])

    for sample in samples:
        for fraction in fractions:
            iteration = 0
            while iteration < int(args.number):
                job = Job.wrapJobFn(subsample_bam, [args.address], "coveragestore", auth_provider, sample, args.seed,
                                    fraction, iteration,
                                    cores=int(config['vcfanno']['num_cores']),
                                    memory="{}G".format(config['vcfanno']['max_mem']))

                # Create workflow from created jobs
                root_job.addChild(job)
                iteration += 1

    # Start workflow execution
    Job.Runner.startToil(root_job, args)
