import pandas as pd
import pytest

from shipping_agent.config import DEFAULT_CONFIG
from shipping_agent.loader import (
    load_customer_master,
    load_document,
    load_voyage_folder,
    normalise_container_no,
)
from shipping_agent.rules import char_distance, is_valid_container_no, iso6346_check_digit, reconcile
from shipping_agent.sample_data import INJECTED


@pytest.mark.parametrize("cn", ["CSQU3054383", "MSKU9070323", "TGHU1234567"])
def test_iso6346_valid(cn):
    assert iso6346_check_digit(cn[:10]) == int(cn[10])
    assert is_valid_container_no(cn)


@pytest.mark.parametrize("cn", ["CSQU3054384", "CSQU305438", "1234ABCDEFG", ""])
def test_iso6346_invalid(cn):
    assert not is_valid_container_no(cn)


def test_char_distance_counts_adjacent_swap_as_one():
    assert char_distance("SEGU0732845", "SEGU0732485") == 1
    assert char_distance("SEGU0732845", "SEGU0732846") == 1
    assert char_distance("SEGU0732845", "SEGU0732000") == 3
    assert char_distance("ABC", "ABCD") == 4


def test_normalise_container_no():
    assert normalise_container_no("msku 123456-5") == "MSKU1234565"
    assert normalise_container_no(None) == ""


def test_column_aliases_are_mapped(tmp_path):
    path = tmp_path / "container_list.csv"
    pd.DataFrame({"CNTR NO": ["CSQU 305438-3"], "ISO Code": ["40hc"], "VGM": ["12,500"],
                  "Seal": ["x1"], "Port of Discharge": ["sgsin"]}).to_csv(path, index=False)
    doc = load_document(path, "container_list")
    row = doc.frame.iloc[0]
    assert row["container_no"] == "CSQU3054383"
    assert row["size_type"] == "40HC"
    assert row["gross_weight_kg"] == 12500
    assert row["pod"] == "SGSIN"


def test_every_injected_issue_is_found(voyage):
    folder, master = voyage
    findings = reconcile(load_voyage_folder(folder), load_customer_master(master), DEFAULT_CONFIG)
    rules = {f.rule for f in findings}
    missing = set(INJECTED) - rules
    assert not missing, f"not detected: {missing}"
    # ids are sequential and ranked by severity
    assert [f.id for f in findings] == [f"F{i:03d}" for i in range(1, len(findings) + 1)]
    sev = [f.severity.value for f in findings]
    assert sev == sorted(sev, key=["HIGH", "MEDIUM", "LOW"].index)


def test_typo_is_reported_once_as_lookalike_not_twice_as_missing(voyage):
    folder, master = voyage
    findings = reconcile(load_voyage_folder(folder), load_customer_master(master), DEFAULT_CONFIG)
    look = [f for f in findings if f.rule == "lookalike_container"]
    assert len(look) == 1
    twin = look[0].actual
    assert not any(f.rule == "missing_in_manifest" and f.container_no == twin for f in findings)


def test_clean_voyage_has_no_findings(tmp_path):
    rows = {"Container No": ["CSQU3054383"], "Size/Type": ["40HC"], "Gross Weight (KG)": [20000],
            "Seal No": ["S1"], "POL": ["IDTPP"], "POD": ["SGSIN"], "Shipper": ["PT A"],
            "Consignee": ["B LTD"], "NPWP": ["0123456789012345"]}
    pd.DataFrame(rows).to_csv(tmp_path / "manifest.csv", index=False)
    pd.DataFrame({"CNTR NO": ["CSQU3054383"], "VGM": [20100], "Seal": ["S1"], "POD": ["SGSIN"]}).to_csv(
        tmp_path / "container_list.csv", index=False)
    findings = reconcile(load_voyage_folder(tmp_path), {"0123456789012345"}, DEFAULT_CONFIG)
    assert findings == []


def test_small_weight_difference_is_tolerated(tmp_path):
    base = {"Container No": ["CSQU3054383"], "Gross Weight (KG)": [20000], "NPWP": ["0123456789012345"]}
    pd.DataFrame(base).to_csv(tmp_path / "manifest.csv", index=False)
    pd.DataFrame({"Container": ["CSQU3054383"], "Weight": [20300]}).to_csv(tmp_path / "stacking_list.csv", index=False)
    findings = reconcile(load_voyage_folder(tmp_path), set(), DEFAULT_CONFIG)
    assert not any(f.rule == "weight_mismatch" for f in findings)


def test_missing_manifest_raises(tmp_path):
    pd.DataFrame({"CNTR NO": ["CSQU3054383"]}).to_csv(tmp_path / "container_list.csv", index=False)
    with pytest.raises(FileNotFoundError):
        load_voyage_folder(tmp_path)
