""" Tests for validation report results, relies on test for loaders passing """
from decimal import DivisionByZero
from pathlib import Path
import os

from pytest import MonkeyPatch
import pytest
from dp_tools.bulkRNASeq.entity import BulkRNASeqSample

from dp_tools.bulkRNASeq.loaders import (
    load_BulkRNASeq_STAGE_00,
    load_BulkRNASeq_STAGE_01,
)
from dp_tools.bulkRNASeq.vv_protocols import BulkRNASeq_VVProtocol_RawData


@pytest.fixture(autouse=True)
def mock_dev_exceptions(monkeypatch):
    monkeypatch.setattr(
        "dp_tools.core.check_model.ALLOWED_DEV_EXCEPTIONS", (DivisionByZero)
    )  # ensure unhandled developer exceptions are raised


def test_bulkRNASeq_STAGE00_validation_paired(caplog, glds194_dataSystem_STAGE00):
    """ This tests validation as it would be run on dataset after demultiplexing """
    CAPLEVEL = 20
    caplog.set_level(CAPLEVEL)
    ds = glds194_dataSystem_STAGE00
    vv_protocol = BulkRNASeq_VVProtocol_RawData(dataset=ds.dataset)

    with caplog.at_level(CAPLEVEL):
        vv_protocol.validate_all()

    assert isinstance(vv_protocol.flags["dataset"], dict)
    assert isinstance(vv_protocol.flags["sample"], dict)
    assert isinstance(vv_protocol.flags["component"], dict)

    # second, run with full validation
    with caplog.at_level(CAPLEVEL):
        caplog.clear()
        with MonkeyPatch.context() as m:
            vv_protocol.validate_all()
            df = vv_protocol.flags_to_df()

            df_verbose = vv_protocol.flags_to_df(schema="verbose")

            # assert that no failing flags were raised
            assert df["flag_code"].max() == 20

            # check if appropriate number of flags are raised
            # Currently:
            #   Dataset check : 2
            #   Sample check : 1 per sample
            #   Component checks :
            #       Reads : 1 per component
            assert len(df) == 2 + len(ds.dataset.samples) * 3


def test_bulkRNASeq_STAGE00_validation_single(caplog, glds48_dataSystem_STAGE00):
    """ This tests validation as it would be run on dataset after demultiplexing """
    CAPLEVEL = 20

    caplog.set_level(CAPLEVEL)
    ds = glds48_dataSystem_STAGE00
    vv_protocol = BulkRNASeq_VVProtocol_RawData(dataset=ds.dataset)

    with MonkeyPatch.context() as m:
        vv_protocol.validate_all()
        df = vv_protocol.flags_to_df()

        df_verbose = vv_protocol.flags_to_df(schema="verbose")

        # assert that no failing flags were raised
        assert df["flag_code"].max() == 20

        # check if appropriate number of flags are raised
        # Currently:
        #   Dataset check : 2
        #   Sample check : 1 per sample
        #   Component checks
        #       Reads : 1 per component (1 per sample)
        assert len(df) == 2 + len(ds.dataset.samples) * 2


def test_bulkRNASeq_STAGE01_validation_paired(caplog, glds194_dataSystem_STAGE00):
    raise NotImplementedError


def test_bulkRNASeq_STAGE01_validation_single(caplog, glds48_dataSystem_STAGE00):
    raise NotImplementedError


def test_bulkRNASeq_STAGE02_validation_paired(caplog, glds194_dataSystem_STAGE00):
    raise NotImplementedError


def test_bulkRNASeq_STAGE02_validation_single(caplog, glds48_dataSystem_STAGE00):
    raise NotImplementedError


def test_bulkRNASeq_STAGE03_validation_paired(caplog, glds194_dataSystem_STAGE00):
    raise NotImplementedError


def test_bulkRNASeq_STAGE03_validation_single(caplog, glds48_dataSystem_STAGE00):
    raise NotImplementedError


def test_bulkRNASeq_STAGE04_validation_paired(caplog, glds194_dataSystem_STAGE00):
    raise NotImplementedError


def test_bulkRNASeq_STAGE04_validation_single(caplog, glds48_dataSystem_STAGE00):
    raise NotImplementedError
