from collections import defaultdict
import copy
import enum
import gzip
import logging
import math
from pathlib import Path
from statistics import mean, median, stdev
import subprocess
from typing import Callable, DefaultDict, Dict, List, Set, Tuple, Union

import pandas as pd
from dp_tools.components.components import GenomeAlignments, RawReadsComponent
from dp_tools.core.entity_model import (
    DataDir,
    DataFile,
    ModuleLevelMQC,
    TemplateDataset,
    TemplateSample,
)

log = logging.getLogger(__name__)

from dp_tools.core.check_model import Check, Flag, FlagCode

# adapted from reference: https://stackoverflow.com/questions/56048627/round-floats-in-a-nested-dictionary-recursively
# used to round values for easier to read messages
def formatfloat(x):
    return "%.3g" % float(x)


def pformat(original_dictionary, function):
    dictionary = copy.deepcopy(
        original_dictionary
    )  # we don't want to override original values
    if isinstance(dictionary, dict):
        new_dict = dict()
        for k, v in dictionary.items():
            new_dict[k] = function(v) if isinstance(v, float) else pformat(v, function)
        return new_dict
    return dictionary


class MIDDLE(enum.Enum):
    mean: Tuple[Callable] = (mean,)
    median: Tuple[Callable] = (median,)

    def __call__(self, *args, **kwargs):
        return self.value[0](*args, **kwargs)


def identify_outliers(
    valueDict: Dict[str, float], standard_deviation_threshold: float, middle: Callable
):
    # determine middle value
    middle_value: float = middle(valueDict.values())
    std_deviation: float = stdev(valueDict.values())

    # init tracker
    # holds the key name and the standard deviations from the middle
    outliers: Dict[str, float] = dict()

    # exit early if std_deviation is zero (i.e. no outliers)
    if std_deviation == 0:
        return outliers

    # check if a value is an outlier
    for key, value in valueDict.items():
        # calculate standard deviations
        num_std_deviations_vector = (value - middle_value) / std_deviation
        # if an outlier, add it to a dict of outliers (include a +/- standard deviations)
        if abs(num_std_deviations_vector) > standard_deviation_threshold:
            outliers[key] = num_std_deviations_vector

    return outliers


# TODO: typedict for thresholds
def identify_values_past_thresholds(thresholds: dict, value: float) -> List[FlagCode]:
    """Return empty list if no codes are raised"""
    VALID_THRESHOLD_TYPES = {"lower", "upper"}
    new_codes = list()
    for threshold in thresholds:
        assert (
            threshold.get("type") in VALID_THRESHOLD_TYPES
        ), f"Invalid threshold type configured: valid options {VALID_THRESHOLD_TYPES} got {threshold.get('type')}"
        if threshold.get("type") == "lower":
            if value < threshold["value"]:
                new_codes.append(threshold["code"])
        elif threshold.get("type") == "upper":
            if value > threshold["value"]:
                new_codes.append(threshold["code"])
    return new_codes


def convert_nan_to_zero(input: Dict[str, Union[float, int]]) -> Dict:
    """Convert any Nan into zero"""
    output = dict()
    for key, value in input.items():
        output[key] = value if not math.isnan(value) else 0
    return output


## Functions that use the following syntax to merge values from general stats:
# "stat1 + stat2" should search and sum the stats
def stat_string_to_value(stat_string: str, mqcData: ModuleLevelMQC) -> float:
    """ "stat1 + stat2" should search and sum the stats"""
    sum = float(0)
    direct_keys = stat_string.split(" + ")
    for direct_key in direct_keys:
        print(direct_key)
        sum += mqcData["General_Stats"][direct_key]
    return sum


## Dataframe and Series specific helper functions
def nonNull(df: pd.DataFrame) -> bool:
    # negation since it checks if any are null
    return ~df.isnull().any(axis=None)


def nonNegative(df: pd.DataFrame) -> bool:
    """This ignores null values, use nonNull to validate that condition"""
    return ((df >= 0) | (df.isnull())).all(axis=None)


def onlyAllowedValues(df: pd.DataFrame, allowed_values: list) -> bool:
    """This ignores null values, use nonNull to validate that condition"""
    return ((df.isin(allowed_values)) | (df.isnull())).all(axis=None)


class SAMPLE_RAWREADS_0001(Check):
    description = (
        "Check that appropriate raw reads components exist. Also check that "
        "All datafiles associated with the components are present. "
        "For paired end studies, this means both rawForwardReads and rawReverseReads "
        "Are attached components. For single end studies, "
        "this means the rawReads component is attached. "
        "For paired end studies, confirms that forward and reverse read counts match."
    )
    flag_desc = {
        FlagCode.GREEN: "All expected raw read files present",
        FlagCode.HALT1: "Missing expected components: {missing_components}",
        FlagCode.HALT2: "Forward and reverse reads counts differ. Forward: ({forward_read_count}) Reverse: ({reverse_read_count})",
        FlagCode.DEV_HANDLED: "Searched for component, but component was not expected by entity model: {unexpected_components}",
    }

    def validate_func(self, sample: TemplateSample) -> Flag:
        # assume passing unless a flag condition arises
        code = FlagCode.GREEN

        # set branching informative parameters based on layout
        if sample.dataset.metadata.paired_end:
            expected_components = ["rawForwardReads", "rawReverseReads"]
            check_read_parity = True
        else:
            expected_components = ["rawReads"]
            check_read_parity = False

        missing_components = list()
        unexpected_components = list()
        for expected_component in expected_components:
            component = getattr(sample, expected_component, None)
            if component == None:
                unexpected_components.append(expected_component)
            if not isinstance(component, RawReadsComponent):
                missing_components.append(expected_component)

        if unexpected_components:
            code = FlagCode.DEV_HANDLED

        if missing_components:
            code = FlagCode.HALT1

        # check parity
        if all([check_read_parity, code == FlagCode.GREEN]):
            if (
                not sample.rawForwardReads.mqcData["FastQC"]["General_Stats"][
                    "total_sequences"
                ]
                == sample.rawReverseReads.mqcData["FastQC"]["General_Stats"][
                    "total_sequences"
                ]
            ):
                code = FlagCode.HALT2

        return Flag(
            check=self,
            codes=code,
            message_args={
                "missing_components": missing_components,
                "forward_read_count": sample.rawForwardReads.mqcData["FastQC"][
                    "General_Stats"
                ]["total_sequences"]
                if code == FlagCode.HALT2
                else None,
                "reverse_read_count": sample.rawReverseReads.mqcData["FastQC"][
                    "General_Stats"
                ]["total_sequences"]
                if code == FlagCode.HALT2
                else None,
            },
        )


class SAMPLE_TRIMREADS_0001(SAMPLE_RAWREADS_0001):
    ...


class COMPONENT_RAWREADS_0001(Check):
    config = {
        "lines_to_check": 200_000_000,
        # attributes names
        "expected_data_files": [
            "fastqGZ",
            "fastQCmultiQCDirZIP",
            "fastqcReportHTML",
            "fastqcReportZIP",
        ],
    }
    description = (
        "Confirms that all read components (e.g. rawForwardReads, trimmedReads) should include the following: "
        "Datafiles of the format: {expected_data_files} related to the reads component. "
        "Additionally, the following checks are performed for each file type: "
        "\tfastq.gz: First {lines_to_check} lines are checked for correct format. "
    )
    flag_desc = {
        FlagCode.GREEN: "Component passes all validation requirements.",
        FlagCode.HALT1: "Missing expected files: {missing_files}",
        FlagCode.HALT2: "Fastq.gz file has issues on lines: {lines_with_issues}",
        FlagCode.HALT3: "Corrupted Fastq.gz file suspected, last line number encountered: {last_line_checked}",
    }

    def validate_func(self: Check, component) -> Flag:
        """Checks fastq lines for expected header content
        Note: Example of header from GLDS-194
        |  ``@J00113:376:HMJMYBBXX:3:1101:26666:1244 1:N:0:NCGCTCGA\n``
        This also assumes the fastq file does NOT split sequence or quality lines
        for any read
        :param component: A ReadsComponent
        """
        # assume passing first
        # overwrite if flag conditions met
        code = FlagCode.GREEN

        # Subcheck: 1 ( can trigger HALT1 )
        # check if expected files exist first
        missing_files: List[Path] = list()
        lines_with_issues: List[int] = list()
        i = 0

        for expected_file in self.config["expected_data_files"]:
            try:
                # check the attribute is exists and is of the proper type
                assert any(
                    [
                        isinstance(getattr(component, expected_file), DataFile),
                        isinstance(getattr(component, expected_file), DataDir),
                    ]
                )
                # check the path exists
                assert getattr(component, expected_file).path.exists()
            except AssertionError:
                code = FlagCode.HALT1
                missing_files.append(expected_file)

        # check if exiting makes sense before next checks
        if code != FlagCode.GREEN:
            return Flag(
                check=self,
                codes=code,
                message_args={
                    "lines_with_issues": lines_with_issues,
                    "last_line_checked": i,
                    "missing_files": missing_files,
                },
            )

        # subcheck: 2 ( can trigger HALT2,HALT3 )
        # check fastq.gz file looks correct
        file = component.fastqGZ.path
        count_lines_to_check = self.config["lines_to_check"]

        if count_lines_to_check == -1:
            count_lines_to_check = float("inf")

        # truncated files raise EOFError
        # catch this as HALT3
        try:
            with gzip.open(file, "rb") as f:
                for i, line in enumerate(f):
                    # checks if lines counted equals the limit input
                    if i + 1 == count_lines_to_check:
                        log.debug(
                            f"Reached {count_lines_to_check} lines, ending line check"
                        )
                        break

                    line = line.decode()
                    # every fourth line should be an identifier
                    expected_identifier_line = i % 4 == 0
                    # check if line is actually an identifier line
                    if expected_identifier_line and line[0] != "@":
                        lines_with_issues.append(i + 1)
                    # update every 20,000,000 reads
                    if i % 20_000_000 == 0:
                        log.debug(f"Checked {i} lines for {file}")
                        pass
            if not len(lines_with_issues) == 0:
                code = FlagCode.HALT2
        except (EOFError, gzip.BadGzipFile):
            code = FlagCode.HALT3

        # return flag
        return Flag(
            check=self,
            codes=code,
            message_args={
                "lines_with_issues": lines_with_issues,
                "last_line_checked": i,
                "missing_files": missing_files,
            },
        )


class COMPONENT_TRIMREADS_0001(COMPONENT_RAWREADS_0001):
    config = {
        "lines_to_check": 200_000_000,
        "expected_data_files": [
            "fastqGZ",
            "fastQCmultiQCDirZIP",
            "fastqcReportHTML",
            "fastqcReportZIP",
            "trimmingReportTXT",
        ],
    }


class COMPONENT_GENOMEALIGNMENTS_0001(Check):
    config = {
        "expected_files": {
            "alignedToTranscriptomeBam": {"samtoolsQuickCheck": True},
            "alignedSortedByCoordBam": {"samtoolsQuickCheck": True},
            "alignedSortedByCoordResortedBam": {"samtoolsQuickCheck": True},
            "alignedSortedByCoordResortedBamIndex": {},
            "logFinal": {},
            "logProgress": {},
            "logFull": {},
            "sjTab": {},
        },
        # Will use the following syntax for combined metrics
        # 'metric1' + 'metric2' + 'metric3'
        # valid types: 'upper', 'lower'
        "general_stats_metrics": {
            "uniquely_mapped_percent + multimapped_percent": [
                {"code": FlagCode.YELLOW1, "type": "lower", "value": 70},
                {"code": FlagCode.RED1, "type": "lower", "value": 50},
            ],
            # DISCUSS: this seems an odd check. Recommending modification
            # Maybe combine with other metrics for more meaningful assessment
            # Ref: https://github.com/J-81/JDO_V-V/blob/b3e0f4734eedabaa7ec99119073cf4e263f0963d/CHECKS.md?plain=1#L192
            "multimapped_toomany_percent + multimapped_percent": [
                {"code": FlagCode.YELLOW1, "type": "lower", "value": 30},
                {"code": FlagCode.RED1, "type": "lower", "value": 15},
            ],
        },
    }
    description = (
        "Check that the following files exists: {expected_files} "
        "Beyond existence, validating the files are not corrupt needs to be performed external to this automated V&V program "
        "Specifically, bam files can be validated using samtools quickcheck (see: http://www.htslib.org/doc/samtools-quickcheck.html) "
        ""
    )
    flag_desc = {
        FlagCode.GREEN: "Component passes all validation requirements.",
        FlagCode.YELLOW1: "Found values beyond defined yellow thresholds: {flagged_values} -> {threshold_config}",
        FlagCode.RED1: "Found values beyond defined red thresholds: {flagged_values} -> {threshold_config}",
        FlagCode.HALT1: "Missing expected files: {missing_files}",
    }

    def UNIMPLEMENTED_samtoolsQuickCheck(self, bamFile: Path) -> str:
        """
        This function is deprecated until getting subprocesses to use conda envs is properly implemented
        Returns error message if an issue is found, empty string otherwise
        """
        # check with coord file with samtools
        process = subprocess.Popen(
            ["samtools", "quickcheck", bamFile],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = process.communicate()
        return stderr.decode()

    def validate_func(
        self: Check, component: GenomeAlignments, mqc_name: str = "STAR"
    ) -> Flag:
        codes = {FlagCode.GREEN}
        missing_files = list()
        flagged_values = dict()

        for expected_file, constraints in self.config["expected_files"].items():  # type: ignore
            # check exists
            if not getattr(component, expected_file).path.is_file():
                codes.add(FlagCode.HALT1)
                missing_files.append(expected_file)

            # check with samtools (as per "samtoolsQuickCheck")
            # if constraints.get("samtoolsQuickCheck"):
            #    self._samtoolsQuickCheck(getattr(component, expected_file).path)

        for key, thresholds in self.config["general_stats_metrics"].items():  # type: ignore
            # key may be a direct general stats key or a stat_string
            # check if direct key
            value = component.mqcData[mqc_name].get(key, None)
            if not value:
                # check if valid stat_string
                value = stat_string_to_value(key, component.mqcData[mqc_name])

            # check against thresholds
            # yellow level outliers
            if new_codes := identify_values_past_thresholds(thresholds, value):
                # add highest severity new code
                codes.add(max(new_codes))
                flagged_values[key] = value

        return Flag(
            check=self,
            codes=codes,
            message_args={
                "missing_files": missing_files,
                "threshold_config": self.config["general_stats_metrics"],
                "flagged_values": flagged_values,
            },
        )


class DATASET_METADATA_0001(Check):
    config = {"expected_metadata_attrs": ["paired_end", "has_ercc"]}
    description = "Checks and reports expected metdata required for processing"
    flag_desc = {
        FlagCode.GREEN: "All expected metadata is accessible and populated. {actual_metadata_fields}",
        FlagCode.HALT1: "Missing expected metadata fields: {missing_metadata_fields}",
    }

    def validate_func(self, dataset: TemplateDataset) -> Flag:
        # assume green unless flag condition met
        code = FlagCode.GREEN

        # set up tracker for expected attributes values
        tracked_metadata = dict()
        # and a tracker for missing attributes
        missing_metadata_fields = list()

        for attr in self.config["expected_metadata_attrs"]:
            attr_value = getattr(dataset.metadata, attr, None)
            if attr_value != None:
                tracked_metadata[attr] = attr_value
            else:
                missing_metadata_fields.append(attr)

        # check if any missing_metadata_fields are present
        if missing_metadata_fields:
            code = FlagCode.HALT1

        return Flag(
            check=self,
            codes=code,
            message_args={
                "actual_metadata_fields": tracked_metadata,
                "missing_metadata_fields": missing_metadata_fields,
            },
        )


class DATASET_RAWREADS_0001(Check):
    config = {
        "metrics": [
            "percent_gc",
            "avg_sequence_length",
            "total_sequences",
            "percent_duplicates",
            # "percent_fails", number of failed FastQC submodules, not a very useful metric for BulkRNASeq
        ],
        "middle": MIDDLE.median,
        "yellow_standard_deviation_threshold": 2,
        "red_standard_deviation_threshold": 4,
        "target_components_by_paired_end": {
            True: ["rawForwardReads", "rawReverseReads"],
            False: ["rawReads"],
        },
    }
    description = (
        "Check that the reads stats (source from FastQC) have no outliers among samples "
        "for the following metrics: {metrics}. "
        "Yellow Flagged Outliers are defined as a being {yellow_standard_deviation_threshold} - {red_standard_deviation_threshold} standard "
        "deviations away from the {middle.name}. "
        "Red Flagged Outliers are defined as a being {red_standard_deviation_threshold}+ standard "
        "deviations away from the {middle.name}. "
    )
    flag_desc = {
        FlagCode.GREEN: "No reads metric outliers detected for {metrics}",
        FlagCode.YELLOW1: "Outliers detected as follows (values are rounded number of standard deviations from middle): {formatted_outliers}",
        FlagCode.RED1: "Outliers detected as follows (values are rounded number of standard deviations from middle): {formatted_outliers}",
    }

    def validate_func(self: Check, dataset: TemplateDataset) -> Flag:
        code = FlagCode.GREEN

        # pull variables from config
        metrics = self.config["metrics"]
        middle = self.config["middle"]
        yellow_threshold = self.config["yellow_standard_deviation_threshold"]
        red_threshold = self.config["red_standard_deviation_threshold"]

        # init trackers for issues
        outliers: DefaultDict[str, Dict[str, float]] = defaultdict(dict)

        # determine reads components in samples
        readsComponents = self.config["target_components_by_paired_end"][
            dataset.metadata.paired_end
        ]

        def format_identifier(sample_name: str, component_str: str) -> str:
            """Add forward and reverse suffix if paired end, add nothing otherwise"""
            return (
                f"{sample_name}:{component_str}"
                if dataset.metadata.paired_end
                else sample_name
            )

        # iterate through metrics (here all pulled from FastQC general stats)
        for readComponent in readsComponents:
            for metric in metrics:
                sampleToMetric: Dict[str, float] = {
                    format_identifier(s.name, readComponent): getattr(
                        s, readComponent
                    ).mqcData["FastQC"]["General_Stats"][metric]
                    for s in dataset.samples.values()
                }

                # ensure any NaN convert to zero as implied by MultiQC
                sampleToMetric = convert_nan_to_zero(sampleToMetric)

                # yellow level outliers
                if outliersForThisMetric := identify_outliers(
                    sampleToMetric,
                    standard_deviation_threshold=yellow_threshold,
                    middle=middle,
                ):
                    if code < FlagCode.YELLOW1:
                        code = FlagCode.YELLOW1
                    outliers[metric] = outliers[metric] | outliersForThisMetric

                # red level outliers
                if outliersForThisMetric := identify_outliers(
                    sampleToMetric,
                    standard_deviation_threshold=red_threshold,
                    middle=middle,
                ):
                    if code < FlagCode.RED1:
                        code = FlagCode.RED1
                    outliers[metric] = outliers[metric] | outliersForThisMetric

        return Flag(
            codes=code,
            check=self,
            message_args={
                "outliers": outliers,
                "metrics": metrics,
                "formatted_outliers": pformat(outliers, formatfloat),
            },
        )


class DATASET_TRIMREADS_0001(DATASET_RAWREADS_0001):
    # overwrite specific config only
    config = DATASET_RAWREADS_0001.config | {
        "target_components_by_paired_end": {
            True: ["trimForwardReads", "trimReverseReads"],
            False: ["trimReads"],
        }
    }


class DATASET_GENOMEALIGNMENTS_0001(Check):
    config = {
        "metrics": [
            # "total_reads", # check in FastQC, but is used to normalize
            # "avg_input_read_length",
            # "uniquely_mapped", # redundant with better metric of percent
            "uniquely_mapped_percent",
            "avg_mapped_read_length",
            # "num_splices",
            # "num_annotated_splices",
            # "num_GTAG_splices",
            # "num_GCAG_splices",
            # "num_ATAC_splices",
            # "num_noncanonical_splices",
            "mismatch_rate",
            "deletion_rate",
            "deletion_length",
            "insertion_rate",
            "insertion_length",
            # "multimapped", # redundant with better metric of percent
            "multimapped_percent",
            # "multimapped_toomany",  # redundant with better metric of percent
            "multimapped_toomany_percent",
            "unmapped_mismatches_percent",
            "unmapped_tooshort_percent",
            "unmapped_other_percent",
            # "unmapped_mismatches", # redundant with better metric of percent
            # "unmapped_tooshort", # redundant with better metric of percent
            # "unmapped_other", # redundant with better metric of percent
        ],
        "middle": MIDDLE.median,
        "yellow_standard_deviation_threshold": 2,
        "red_standard_deviation_threshold": 4,
    }
    description = (
        "Check that the genome alignment stats (source from STAR logs) have no outliers among samples "
        "for the following metrics: {metrics}. "
        "Yellow Flagged Outliers are defined as a being {yellow_standard_deviation_threshold} - {red_standard_deviation_threshold} standard "
        "deviations away from the {middle.name}. "
        "Red Flagged Outliers are defined as a being {red_standard_deviation_threshold}+ standard "
        "deviations away from the {middle.name}. "
    )
    flag_desc = {
        FlagCode.GREEN: "No genome alignment metric outliers detected for {metrics}",
        FlagCode.YELLOW1: "Outliers detected as follows (values are rounded number of standard deviations from middle): {formatted_outliers}",
        FlagCode.RED1: "Outliers detected as follows (values are rounded number of standard deviations from middle): {formatted_outliers}",
    }

    def validate_func(self: Check, dataset: TemplateDataset) -> Flag:
        code = FlagCode.GREEN

        # pull variables from config
        metrics = self.config["metrics"]
        middle = self.config["middle"]
        yellow_threshold = self.config["yellow_standard_deviation_threshold"]
        red_threshold = self.config["red_standard_deviation_threshold"]

        # init trackers for issues
        outliers: DefaultDict[str, Dict[str, float]] = defaultdict(dict)

        # determine reads components in samples
        targetComponents = ["genomeAlignments"]

        # iterate through metrics (here all pulled from FastQC general stats)
        for targetComponent in targetComponents:
            for metric in metrics:
                sampleToMetric: Dict[str, float] = {
                    s.name: getattr(s, targetComponent).mqcData["STAR"][
                        "General_Stats"
                    ][metric]
                    for s in dataset.samples.values()
                }
                # ensure any NaN convert to zero as implied by MultiQC
                sampleToMetric = convert_nan_to_zero(sampleToMetric)

                # yellow level outliers
                if outliersForThisMetric := identify_outliers(
                    sampleToMetric,
                    standard_deviation_threshold=yellow_threshold,
                    middle=middle,
                ):
                    if code < FlagCode.YELLOW1:
                        code = FlagCode.YELLOW1
                    outliers[metric] = outliers[metric] | outliersForThisMetric

                # red level outliers
                if outliersForThisMetric := identify_outliers(
                    sampleToMetric,
                    standard_deviation_threshold=red_threshold,
                    middle=middle,
                ):
                    if code < FlagCode.RED1:
                        code = FlagCode.RED1
                    outliers[metric] = outliers[metric] | outliersForThisMetric

        return Flag(
            codes=code,
            check=self,
            message_args={
                "outliers": outliers,
                "metrics": metrics,
                "formatted_outliers": pformat(outliers, formatfloat),
            },
        )


class DATASET_RSEQCANALYSIS_0001(Check):
    config = {
        "plots_all": ["Read Distribution", "Infer experiment", "Gene Body Coverage"],
        "plot_paired_end": ["Inner Distance"],
        "middle": MIDDLE.median,
        "yellow_standard_deviation_threshold": 2,
        "red_standard_deviation_threshold": 4,
        "stranded_assessment_range": {"min": 75, "max": 100},  # percents
        "halt_ambiguous_dominant_strandedness_range": {
            "min": 60,
            "max": 75,
        },  # percents
        "unstranded_assessment_range": {"min": 40, "max": 60},  # percents
        "valid_dominant_strandedness_assessments": [
            "Sense (% Tags)",
            "Antisense (% Tags)",
        ],  # this leaves out undetermined, which should raise alarms if it is the dominant assessment
    }
    description = (
        "Check that the rseqc analysis stats (sourced from the rseqc logs) have no outlier values among samples "
        "for the following plots: {plots_all} (Paired end only: {plot_paired_end}). "
        "Yellow Flagged Outliers are defined as a being {yellow_standard_deviation_threshold} - {red_standard_deviation_threshold} standard "
        "deviations away from the {middle.name}. "
        "Red Flagged Outliers are defined as a being {red_standard_deviation_threshold}+ standard "
        "deviations away from the {middle.name}. "
        "Additionally the following is assessed for infer experiment strandedess metrics: "
        "A Halt Flag is raised in the case that the dominant strandessness is between "
        "{halt_ambiguous_dominant_strandedness_range} "
        "Note: the 'dominant strandedness' is the max(datasetwide_median(antisense), datasetwide_median(sense)) "
        "Valid assessments include {valid_dominant_strandedness_assessments}, other assessments (e.g. 'undetermined') will raise a Halting flag "
    )
    flag_desc = {
        FlagCode.GREEN: "No rseqc analysis metric outliers detected for {metrics}",
        FlagCode.YELLOW1: "Outliers detected as follows (values are rounded number of standard deviations from middle): {formatted_outliers}",
        FlagCode.RED1: "Outliers detected as follows (values are rounded number of standard deviations from middle): {formatted_outliers}",
        FlagCode.RED2: "At least one sample is outside the dominant strandedness assignment range: {samples_outside_range}",
        FlagCode.HALT1: "The dominant strandedness is {dominant_strandedness}, this is lower than the halting flag threshold.",
        FlagCode.HALT2: "The dominant strandedness is {dominant_strandedness} which is not a invalid assessment.",
    }

    def validate_func(self: Check, dataset: TemplateDataset) -> Flag:
        codes = {FlagCode.GREEN}

        # pull variables from config
        targetPlotsAll = self.config["plots_all"]
        targetPlotsPairedEnd = self.config["plot_paired_end"]
        middle = self.config["middle"]
        yellow_threshold = self.config["yellow_standard_deviation_threshold"]
        red_threshold = self.config["red_standard_deviation_threshold"]

        # init trackers for issues
        outliers: DefaultDict[str, Dict[str, float]] = defaultdict(dict)

        # extend with paired end specific plot if appropriate
        targetPlots = targetPlotsAll
        if dataset.metadata.paired_end:
            targetPlots.extend(targetPlotsPairedEnd)

        # iterate through metrics (here all pulled from FastQC general stats)
        for plot_name in targetPlots:
            # extract dataframe of all samples
            df = dataset.getMQCDataFrame(
                sample_component="rSeQCAnalysis", mqc_module="RSeQC", mqc_plot=plot_name
            )

            # convert to samplewise dicts
            metricToSampleToMetricValue: Dict[str, Dict[str, float]] = df.to_dict()

            for metricName, sampleToMetricValue in metricToSampleToMetricValue.items():
                # ensure any NaN convert to zero as implied by MultiQC
                sampleToMetricValue = convert_nan_to_zero(sampleToMetricValue)

                # yellow level outliers
                if outliersForThisMetric := identify_outliers(
                    sampleToMetricValue,
                    standard_deviation_threshold=yellow_threshold,
                    middle=middle,
                ):
                    if max(codes) < FlagCode.YELLOW1:
                        codes.add(FlagCode.YELLOW1)
                    outliers[metricName] = outliers[metricName] | outliersForThisMetric

                # red level outliers
                if outliersForThisMetric := identify_outliers(
                    sampleToMetricValue,
                    standard_deviation_threshold=red_threshold,
                    middle=middle,
                ):
                    if max(codes) < FlagCode.RED1:
                        codes.add(FlagCode.RED1)
                        # remove lower FlagCode YELLOW1
                        codes.remove(FlagCode.YELLOW1)
                    outliers[metricName] = outliers[metricName] | outliersForThisMetric

        def get_median_strandedness(dataset: TemplateDataset) -> tuple[str, float]:
            df = dataset.getMQCDataFrame(
                sample_component="rSeQCAnalysis",
                mqc_module="RSeQC",
                mqc_plot="Infer experiment",
            ).fillna(0) # Nan is a zero for this MultiQC table

            median_strandedness = df.median().to_dict()

            return median_strandedness

        median_strandedness = get_median_strandedness(dataset)

        # check if dominant assessment is valid
        strand_assessment: str = max(median_strandedness, key=lambda k: median_strandedness[k])
        if strand_assessment not in self.config['valid_dominant_strandedness_assessments']:
            codes.add(FlagCode.HALT2)

        # flag based on thresholds
        assessment_value: float = median_strandedness[strand_assessment]

        is_stranded: bool = self.config['stranded_assessment_range']['max'] > assessment_value > self.config['stranded_assessment_range']['min']
        is_unstranded: bool = self.config['unstranded_assessment_range']['max'] > assessment_value > self.config['unstranded_assessment_range']['min']            

        def determine_samples_outside_range(dataset: TemplateDataset, min: float, max: float) -> list[str]:
            df = dataset.getMQCDataFrame(
                sample_component="rSeQCAnalysis",
                mqc_module="RSeQC",
                mqc_plot="Infer experiment",
            ).fillna(0) # Nan is a zero for this MultiQC table

            return df.index[df[strand_assessment].between(min, max) == False].to_list()

        # Catalog and flag any samples outside of range
        # flags based on samples that are out of the assessment range
        samples_outside_range: list[str]
        if is_stranded:
            samples_outside_range = determine_samples_outside_range(dataset, self.config['stranded_assessment_range']['min'], self.config['stranded_assessment_range']['max'])
        elif is_unstranded:
            samples_outside_range = determine_samples_outside_range(dataset, self.config['unstranded_assessment_range']['min'], self.config['unstranded_assessment_range']['max'])
        else: # this means that the standing is ambiguous
            samples_outside_range = list()
            codes.add(FlagCode.HALT1)

        if len(samples_outside_range) != 0 :
            codes.add(FlagCode.RED2)

        return Flag(
            codes=codes,
            check=self,
            message_args={
                "outliers": outliers,
                "formatted_outliers": pformat(outliers, formatfloat),
                "dominant_strandedness": (strand_assessment, assessment_value),
                "samples_outside_range": samples_outside_range,
            },
        )


class DATASET_GENECOUNTS_0001(Check):
    config = {
        "metrics": [
            "Unalignable",
            "Alignable",
            "Filtered",
            "Total",
            "alignable_percent",
            "Unique",
            "Multi",
            "Uncertain",
        ],
        "middle": MIDDLE.median,
        "yellow_standard_deviation_threshold": 2,
        "red_standard_deviation_threshold": 4,
    }
    description = (
        "Check that the gene counts alignments (source from the RSEM logs) have no outlier values among samples "
        "for the following metrics: {metrics} "
        "Yellow Flagged Outliers are defined as a being {yellow_standard_deviation_threshold} - {red_standard_deviation_threshold} standard "
        "deviations away from the {middle.name}. "
        "Red Flagged Outliers are defined as a being {red_standard_deviation_threshold}+ standard "
        "deviations away from the {middle.name}. "
    )
    flag_desc = {
        FlagCode.GREEN: "No gene count mapping metric outliers detected for {metrics}",
        FlagCode.YELLOW1: "Outliers detected as follows (values are rounded number of standard deviations from middle): {formatted_outliers}",
        FlagCode.RED1: "Outliers detected as follows (values are rounded number of standard deviations from middle): {formatted_outliers}",
    }

    def validate_func(self: Check, dataset: TemplateDataset) -> Flag:
        codes = {FlagCode.GREEN}

        # pull variables from config
        metrics = self.config["metrics"]
        middle = self.config["middle"]
        yellow_threshold = self.config["yellow_standard_deviation_threshold"]
        red_threshold = self.config["red_standard_deviation_threshold"]

        # init trackers for issues
        outliers: DefaultDict[str, Dict[str, float]] = defaultdict(dict)

        # extract dataframe of general stats
        df = dataset.getMQCDataFrame(
            sample_component="geneCounts", mqc_module="Rsem", mqc_plot="general_stats"
        )

        # iterate through metrics (here all pulled from FastQC general stats)
        for metric_name in metrics:
            sampleToMetricValue = df[[metric_name]].to_dict()[metric_name]
            # ensure any NaN convert to zero as implied by MultiQC
            sampleToMetricValue = convert_nan_to_zero(sampleToMetricValue)

            # yellow level outliers
            if outliersForThisMetric := identify_outliers(
                sampleToMetricValue,
                standard_deviation_threshold=yellow_threshold,
                middle=middle,
            ):
                if max(codes) < FlagCode.YELLOW1:
                    codes.add(FlagCode.YELLOW1)
                    outliers[metric_name] = (
                        outliers[metric_name] | outliersForThisMetric
                    )

            # red level outliers
            if outliersForThisMetric := identify_outliers(
                sampleToMetricValue,
                standard_deviation_threshold=red_threshold,
                middle=middle,
            ):
                if max(codes) < FlagCode.RED1:
                    codes.add(FlagCode.RED1)
                    outliers[metric_name] = (
                        outliers[metric_name] | outliersForThisMetric
                    )

        return Flag(
            codes=codes,
            check=self,
            message_args={
                "outliers": outliers,
                "formatted_outliers": pformat(outliers, formatfloat),
                "metrics": metrics,
            },
        )


# TODO: Flag message gets really messy, convert into a json like string for easier reading/parsing
# TODO: Check for extra unexpected columns, these should give clues to names differences
class DATASET_DIFFERENTIALGENEEXPRESSION_0001(Check):
    config = {
        "expected_tables": [
            "differential_expression.csv",
            "visualization_output_table.csv",
            "visualization_PCA_table.csv",
        ],
        # Expected column name, but dependent on dataset organism
        "dge_table_master_annotation_keys": {
            "Arobidopsis thaliana":"TAIR",
            "_DEFAULT":"ENSEMBL"
        },
        "dge_table_expected_annotation_columns": [
            "SYMBOL",
            "GENENAME",
            "REFSEQ",
            "ENTREZID",
            "STRING_id",
            "GOSLIM_IDS",
        ],
        # includes column specific constraints
        # these prefix as follows {prefix}{pairWiseFactorGroupComparison}
        "pairwise_columns_prefixes": {
            "Log2fc_": {"nonNull": True},
            "Stat_": {"nonNull": True},
            # can be removed from analysis before p-value and adj-p-value assessed
            # ref: https://bioconductor.org/packages/release/bioc/vignettes/DESeq2/inst/doc/DESeq2.html#why-are-some-p-values-set-to-na
            "P.value_": {"nonNegative": True, "nonNull": False},
            "Adj.p.value_": {"nonNegative": True, "nonNull": False},
        },
        "viz_pairwise_columns_prefixes": {
            "Log2_Adj.p.value_": {"nonNull": False},
            "Sig.1_": {"allowedValues": [False, True], "nonNull": False},
            "Sig.05_": {"allowedValues": [False, True], "nonNull": False},
            "Log2_P.value_": {"nonNegative": False, "nonNull": False},
            "Updown_": {"allowedValues": [1, 0, -1], "nonNull": True},
        },
        # these prefix as follows {prefix}{FactorGroup}
        "group_factorwise_columns_prefixes": {
            "Group.Mean_": {"nonNull": True, "nonNegative": True},
            "Group.Stdev_": {"nonNull": True, "nonNegative": True},
        },
        "fixed_stats_columns": {
            "All.mean": {"nonNull": True, "nonNegative": True},
            "All.stdev": {"nonNull": True, "nonNegative": True},
            "LRT.p.value": {"nonNull": False, "nonNegative": True},
        },
        "sample_counts_constraints": {"nonNegative": True},
        "expected_vis_pca_columns": [
            "PC1",
            "PC2",
        ],  # more may be included but these are REQUIRED
        "float_tolerance": 0.0001, # PERCENT
        # TODO: DISCUSS, these baseline values, should indicate a very heavy left-hand skewed histogram of differences - JDO
        "log2fc_cross_method_percent_difference_threshold": 10, # PERCENT
        "log2fc_cross_method_tolerance_percent": 60, # PERCENT
        # "middle": MIDDLE.median,
        # "yellow_standard_deviation_threshold": 2,
        # "red_standard_deviation_threshold": 4,
    }
    description = (
        "Check that the differential expression outputs exist (source from the deseq2 script) and  "
        "the following tables: {expected_tables}.  "
        "For studies with ERCC spike-in, performs the same check on analogous tables. "
        "Additional performs the file specific validations: "
        "- contrasts.csv: Includes all the existing comparison groups (based on factor values in the metadata) and is formatted correctly"
        "- differential_expression.csv:  Includes expected annotation columns {dge_table_expected_annotation_columns}, includes a master annotation key "
        "column dependent on the dataset organism as follows: {dge_table_master_annotation_keys} ,"
        "includes sample count columns for all samples, all sample count values are non-negative, "
        "all pairwise comparision columns exist with the following prefixes and adhere to the following constraints: {pairwise_columns_prefixes} "
        "all groupFactorWise statistics columns exists with the following prefixes and adhere to the following constraints: {group_factorwise_columns_prefixes} "
        "all fixed statistics columns exist and adhere to the following constraints: {fixed_stats_columns} "
        " - visualization_PCA_table.csv: All samples in index and at the following columns exist {expected_vis_pca_columns} "
        " - visualization_output_table.csv: Performs same checks as differential_expression.csv as well as, "
        "ensuring the additional pairwise comparision columns exist with the following prefixes and "
        "adhere to the following constraints: {expected_vis_pca_columns}. "
        "Confirms that gene counts between differential expression table and normalized counts tables are the same. "
        "Confirms that computations match expectations with respect to following operations: (Float tolerance: +/-{float_tolerance} %)"
        "- Group means are correctly computed from normalized counts "
        "- log2FC values (computed with DESeq2's MLE approach) are comparable to direct computation with log2( mean(group1) / mean(group2) ), specifically "
        "checking if at least {log2fc_cross_method_tolerance_percent} % of all genes have absolute percent differences between methods "
        "less than {log2fc_cross_method_percent_difference_threshold} % "
    )
    flag_desc = {
        FlagCode.GREEN: "All described elements checked and no issues arose",
        FlagCode.HALT1: "Contrasts file does not match expectations based on metadata: Error Message(s): {contrasts_err_msg}",
        FlagCode.HALT2: "Differential expression file does not match expectations: Error Message(s): {differential_expression_table_err_msg}",
        FlagCode.HALT3: "Viz PCA file does not match expectations: Error Message(s): {viz_pca_err_msg}",
        FlagCode.HALT4: "Viz output table file does not match expectations: Error Message(s): {viz_output_table_err_msg}",
    }

    def _contrasts_check(self, dataset: TemplateDataset, componentTarget: str) -> str:
        """Performs contrasts specific subcheck

        Returns empty string if no issues are found
        Returns an error message (string) otherwise
        """
        # extract target Component
        target_component = getattr(dataset, componentTarget)

        err_msg = ""
        # extract dicts for deseq2 contrasts and the metadata formatted one here
        # make sure to read in explicit index column for deseq2
        dict_deseq2: Dict = pd.read_csv(
            target_component.contrastsCSV.path, index_col=0
        ).to_dict(orient="list")
        dict_data_model: Dict = dataset.metadata.contrasts.to_dict(orient="list")

        # check that all headers are present
        deseq2_headers = set(dict_deseq2.keys())
        data_model_headers = set(dict_data_model.keys())
        if deseq2_headers != data_model_headers:
            err_msg += f"Header disparity! Extra deseq2 headers: {deseq2_headers - data_model_headers} Extra metadata headers: {data_model_headers - deseq2_headers}"
            # return early, if headers mismatch no point in checking column content
            return err_msg

        # check contents of each column matches expecatation (group1 and group2 formatted as expected)
        # this also rechecks headers (keys) but that is caught in the prior validation
        if dict_deseq2 != dict_data_model:
            err_msg += f"Rows don't match expectations. Deseq2: {dict_deseq2}. DataModel (from metadata source): {dict_data_model}"
        return err_msg

    def _differential_expression_table_check(
        self,
        dataset: TemplateDataset,
        componentTarget: str,
        componentDataAsset: str = "annotatedTableCSV",
    ) -> str:
        err_msg = ""
        target_component = getattr(dataset, componentTarget)
        target_data_asset = getattr(target_component, componentDataAsset)

        # read in dataframe
        df_dge = pd.read_csv(target_data_asset.path)

        # check all constant columns exist
        missing_constant_columns: set
        master_key = self.config["dge_table_master_annotation_keys"].get(dataset.metadata.organism, self.config["dge_table_master_annotation_keys"]["_DEFAULT"])
        expected_columns: list = self.config["dge_table_expected_annotation_columns"] + [master_key] # type: ignore
        if missing_constant_columns := set(expected_columns) - set(df_dge.columns):
            err_msg += f"Annotation Columns missing: {missing_constant_columns}"

        # check all sample counts columns exist
        expected_samples = set(dataset.samples.keys())
        if missing_samples := expected_samples - set(df_dge.columns):
            err_msg += f"Sample Count Columns missing: {missing_samples}"
        # check that they met constraints
        # all sample column counts are not negative
        if not (df_dge[list(expected_samples)] >= 0).all(axis=None):
            err_msg += (
                f"Sample Count Columns include negative values: {missing_samples}"
            )

        # check all expected statistic columns present
        # pairwise comparison level
        pairwise_comparisons = dataset.metadata.contrasts.columns
        for statistical_prefix, constraints in self.config[
            "pairwise_columns_prefixes"
        ].items():  # type: ignore
            target_cols: list = [
                f"{statistical_prefix}{comparison}"
                for comparison in pairwise_comparisons
            ]
            # check existense first and bail if any don't exist
            if missing_cols := set(target_cols) - set(df_dge.columns):
                err_msg += f"Missing pairwise statistical column(s): {missing_cols}"
                continue
            target_df_subset: pd.DataFrame = df_dge[target_cols]

            # check non null constraint
            if constraints.get("nonNull") and nonNull(target_df_subset) == False:
                err_msg += f"At least one value in columns {target_cols} fails nonNull constraint."
            # check non negative constraint
            if (
                constraints.get("nonNegative")
                and nonNegative(target_df_subset) == False
            ):
                err_msg += f"At least one value in columns {target_cols} fails nonNegative constraint."

        # factorGroup level
        factorGroups = list(
            set(dataset.metadata.factor_groups.values())
        )  # list-set to dedupe
        for statistical_prefix, constraints in self.config[
            "group_factorwise_columns_prefixes"
        ].items():  # type: ignore
            target_cols = [f"{statistical_prefix}{group}" for group in factorGroups]
            # check existense first and bail if any don't exist
            if missing_cols := set(target_cols) - set(df_dge.columns):
                err_msg += f"Missing groupFactor statistical column(s): {missing_cols}"
                continue
            target_df_subset = df_dge[target_cols]

            # check non null constraint
            if constraints.get("nonNull") and nonNull(target_df_subset) == False:
                err_msg += f"At least one value in columns {target_cols} fails nonNull constraint."
            # check non negative constraint
            if (
                constraints.get("nonNegative")
                and nonNegative(target_df_subset) == False
            ):
                err_msg += f"At least one value in columns {target_cols} fails nonNegative constraint."

        # fixed stat columns level
        for target_col, constraints in self.config["fixed_stats_columns"].items():  # type: ignore
            # check existense first and bail if any don't exist
            if missing_cols := {target_col} - set(df_dge.columns):
                err_msg += f"Missing fixed statistical column(s): {missing_cols}"
                continue
            target_df_subset = df_dge[target_col]

            # check non null constraint
            if constraints.get("nonNull") and nonNull(target_df_subset) == False:
                err_msg += f"At least one value in column ['{target_col}'] fails nonNull constraint."
            # check non negative constraint
            if (
                constraints.get("nonNegative")
                and nonNegative(target_df_subset) == False
            ):
                err_msg += f"At least one value in column ['{target_col}'] fails nonNegative constraint."

        # mathematical checks
        groups: list[str] = list({group for group in dataset.metadata.factor_groups.values()})
        # check means are computed correctly
        for query_group in groups:
            query_column = f"Group.Mean_{query_group}"
            group_samples = [sample for sample, this_group in dataset.metadata.factor_groups.items() if this_group == query_group]
            abs_percent_difference = abs((((df_dge[group_samples].mean(axis="columns") - df_dge[query_column])/df_dge[query_column]) * 100 ))
            within_tolerance = abs_percent_difference < self.config["float_tolerance"]
            if not within_tolerance.all() == True:
                err_msg += f"Group Mean value in table is out of float tolerance. This means {query_group} has improperly computed values"
        # check that log2FC are within a reasonable range
        # the log2FC computation within DESEQ2 is NOT directly computed from the ratio of group means
        # 
        for comparision in dataset.metadata.contrasts.columns:
            query_column = f"Log2fc_{comparision}"
            group1_mean_col = "Group.Mean_" + comparision.split(')v(')[0] + ")" # Uses parens and adds them back to prevent slicing on 'v' within factor names
            group2_mean_col = "Group.Mean_" + "(" + comparision.split(')v(')[1]
            computed_log2fc = (df_dge[group1_mean_col] / df_dge[group2_mean_col]).apply(math.log, args=[2])
            abs_percent_difference =  abs( ( ( computed_log2fc - df_dge[query_column]) / df_dge[query_column]) * 100 )
            percent_within_tolerance = mean(abs_percent_difference < self.config["log2fc_cross_method_percent_difference_threshold"]) * 100
            # flag if not enough within tolerance
            if percent_within_tolerance < self.config["log2fc_cross_method_tolerance_percent"]:
                err_msg += (
                    f"For comparison: '{comparision}' {percent_within_tolerance:.2f} % of genes have absolute percent differences "
                    f"(between log2fc direct computation and DESeq2's approach) "
                    f"less than {self.config['log2fc_cross_method_percent_difference_threshold']} % which does not met the minimum percentage "
                    f"({self.config['log2fc_cross_method_tolerance_percent']} %) of genes required.  "
                    f"This may indicate misassigned or misaligned columns. "
                )

        return err_msg

    def _viz_pca_table_check(
        self,
        dataset: TemplateDataset,
        componentTarget: str,
        dataAssetTarget: str = "visualizationPCATableCSV",
    ) -> str:
        err_msg = ""
        target_component = getattr(dataset, componentTarget)
        target_asset = getattr(target_component, dataAssetTarget)

        # read into dataframe
        df = pd.read_csv(target_asset.path, index_col=0)

        # check all samples included
        if missing_samples := set(dataset.samples.keys()) - set(df.index):
            err_msg += f"Missing samples in index: {missing_samples}"

        # check all expected columns exist
        if missing_cols := set(self.config["expected_vis_pca_columns"]) - set(df.columns):  # type: ignore
            err_msg += f"Missing expected columns: {missing_cols}"

        return err_msg

    def _viz_output_table_check(
        self, dataset: TemplateDataset, componentTarget: str
    ) -> str:
        """Since this effectively extends the differential expression table,
        run that first and build on the error message as needed"""
        err_msg = self._differential_expression_table_check(
            dataset, componentTarget, componentDataAsset="visualizationTableCSV"
        )

        target_component = getattr(dataset, componentTarget)
        target_data_asset = getattr(target_component, "visualizationTableCSV")

        # read in dataframe
        df = pd.read_csv(target_data_asset.path)

        # check all expected columns exists (all unique to the viz table)
        # check all expected statistic columns present
        # pairwise comparison level
        pairwise_comparisons = dataset.metadata.contrasts.columns
        for statistical_prefix, constraints in self.config[
            "viz_pairwise_columns_prefixes"
        ].items():  # type: ignore
            target_cols: list = [
                f"{statistical_prefix}{comparison}"
                for comparison in pairwise_comparisons
            ]
            # check existense first and bail if any don't exist
            if missing_cols := set(target_cols) - set(df.columns):
                err_msg += f"Missing pairwise statistical column(s): {missing_cols}"
                continue
            target_df_subset: pd.DataFrame = df[target_cols]

            # check non null constraint
            if constraints.get("nonNull") and nonNull(target_df_subset) == False:
                err_msg += f"At least one value in columns {target_cols} fails nonNull constraint."

            # check non negative constraint
            if (
                constraints.get("nonNegative")
                and nonNegative(target_df_subset) == False
            ):
                err_msg += f"At least one value in columns {target_cols} fails nonNegative constraint."

            # check allowed values constraint
            if (
                constraints.get("allowedValues")
                and onlyAllowedValues(
                    target_df_subset, constraints.get("allowedValues")
                )
                == False
            ):
                err_msg += f"At least one value in columns {target_cols} fails allowedValues constraint (allowed values: {constraints.get('allowedValues')})."

        return err_msg

    def validate_func(self: Check, dataset: TemplateDataset) -> Flag:
        dataset.metadata.contrasts
        codes = {FlagCode.GREEN}

        target_components = ["differentialGeneExpression"]
        if dataset.metadata.has_ercc:
            target_components.append("differentialGeneExpressionERCC")

        # holds component and subcheck specific error messages
        err_msgs: Dict = defaultdict(dict)

        for target_component in target_components:
            # perform contrasts file subcheck
            contrasts_result = self._contrasts_check(dataset, target_component)
            if contrasts_result != "":
                codes.add(FlagCode.HALT1)
            err_msgs["contrasts"][target_component] = contrasts_result

            # perform differential expression file subcheck
            differential_expression_result = self._differential_expression_table_check(
                dataset, target_component
            )
            if differential_expression_result != "":
                codes.add(FlagCode.HALT2)
            err_msgs["differential_expression"][
                target_component
            ] = differential_expression_result

            # perform viz PCA file subcheck
            viz_pca_result = self._viz_pca_table_check(dataset, target_component)
            if viz_pca_result != "":
                codes.add(FlagCode.HALT3)
            err_msgs["viz_pca"][target_component] = viz_pca_result

            # perform viz PCA file subcheck
            viz_output_table_result = self._viz_output_table_check(
                dataset, target_component
            )
            if viz_output_table_result != "":
                codes.add(FlagCode.HALT4)
            err_msgs["viz_output_table"][target_component] = viz_output_table_result

        return Flag(
            codes=codes,
            check=self,
            message_args={
                "contrasts_err_msg": "::".join(
                    [f"{k}->{v}" for k, v in err_msgs["contrasts"].items()]
                ),
                "differential_expression_table_err_msg": "::".join(
                    [
                        f"{k}->{v}"
                        for k, v in err_msgs["differential_expression"].items()
                    ]
                ),
                "viz_pca_table_err_msg": "::".join(
                    [f"{k}->{v}" for k, v in err_msgs["viz_pca"].items()]
                ),
                "viz_output_table_err_msg": "::".join(
                    [f"{k}->{v}" for k, v in err_msgs["viz_output_table"].items()]
                ),
            },
        )
