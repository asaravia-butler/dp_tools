from pathlib import Path
import os

from dp_tools.loaders import load_from_bulk_rnaseq_raw_dir
from dp_tools.metadata import Runsheet


# set for testing
TEST_DIR = Path(os.environ["TEST_ASSETS_DIR"])


def test_from_bulk_rnaseq_raw_dir(caplog):
    target_data_dir = TEST_DIR / "GLDS-194"

    target_runsheet = (
        TEST_DIR
        / "GLDS-194"
        / "Metadata"
        / "AST_autogen_template_RNASeq_RCP_GLDS-194_RNASeq_runsheet.csv"
    )

    caplog.set_level(0)
    ds = load_from_bulk_rnaseq_raw_dir(
        target_data_dir, metadata=Runsheet(target_runsheet)
    )

    # pull dataset
    dataset = ds.datasets["GLDS-194:BulkRNASeq"]

    assert list(dataset.samples.keys()) == [
        "Mmus_BAL-TAL_LRTN_BSL_Rep1_B7",
        "Mmus_BAL-TAL_RRTN_BSL_Rep2_B8",
        "Mmus_BAL-TAL_RRTN_BSL_Rep3_B9",
        "Mmus_BAL-TAL_RRTN_BSL_Rep4_B10",
        "Mmus_BAL-TAL_LRTN_GC_Rep1_G6",
        "Mmus_BAL-TAL_LRTN_GC_Rep2_G8",
        "Mmus_BAL-TAL_LRTN_GC_Rep3_G9",
        "Mmus_BAL-TAL_RRTN_GC_Rep4_G10",
        "Mmus_BAL-TAL_LRTN_FLT_Rep1_F6",
        "Mmus_BAL-TAL_LRTN_FLT_Rep2_F7",
        "Mmus_BAL-TAL_LRTN_FLT_Rep3_F8",
        "Mmus_BAL-TAL_LRTN_FLT_Rep4_F9",
        "Mmus_BAL-TAL_LRTN_FLT_Rep5_F10",
    ]

    print("debug")

