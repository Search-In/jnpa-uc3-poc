"""Unit tests for eir_ocr extractors / normalize (no tesseract required)."""
from __future__ import annotations

import pytest

from eir_ocr.extract import extract_eir_fields, high_value_hits
from eir_ocr.normalize import (
    fold_confusion,
    is_valid_container,
    is_valid_plate,
    norm_alnum,
)


def test_norm_and_confusion():
    assert norm_alnum("MH-43 BX 1488") == "MH43BX1488"
    assert fold_confusion("IGTK01") == fold_confusion("1GTKO1")


def test_validators():
    assert is_valid_container("MSMU1908508")
    assert is_valid_container("MRKU5014206")
    assert not is_valid_container("MSMU190")
    assert is_valid_plate("MH43BX1488")
    assert is_valid_plate("mh43bx1488")


def test_extract_psa_style_snippet():
    text = """
    PSA Mumbai BMCT
    Shipping Agent: MSC
    EIR No: 4339869
    LIC No: MH43BX1488
    Truck Company: TRANSTA
    Container No: MSMU1908508
    ISO Code: 2210
    Gross Weight: 24.6 t
    Status: Full
    Vessel/Via: SAV/S0696
    To/From: CLP CFS
    BAT No: B723
    """
    fields = extract_eir_fields(text)
    assert fields["ContainerNo"].value == "MSMU1908508"
    assert fields["LICNo"].value == "MH43BX1488"
    assert fields["EIRNo"].value == "4339869"
    assert fields["ShippingAgent"].value == "MSC"
    assert fields["ISOCode"].value == "2210"
    assert high_value_hits(fields)


def test_extract_gateway_style_snippet():
    text = """
    Gateway Terminals India Pvt Ltd
    EIR Deliver Import
    Container: MRKU5014206
    BAT ID: D391
    Line: MAE
    Trans ID: 5599372
    Status: FCL
    ISO: 4510
    Gross Weight: 31.81 MT
    Seal1: OM0130728
    Truck No: MH43BX1488
    Driver: BABALU KUMAR
    Vessel: ALEXANDRA MAERSK
    """
    fields = extract_eir_fields(text)
    assert fields["ContainerNo"].value == "MRKU5014206"
    assert fields["LICNo"].value == "MH43BX1488"
    assert fields["Line"].value == "MAE"
    assert fields["BATNo"].value == "D391"
    assert "31.81" in fields["GrossWeight"].value


def test_extract_gateway_ocr_noise():
    """Mirrors real Gateway OCR: CTR/@ glyphs, CAT ID, Trk No, VIA noise."""
    text = """
    GATEWAY TERMINALS INDIA PVT.LTD
    CTR No :MRKUS@14206
    CAT ID) = D391
    Line : MAE
    Trans ID :5599372
        Status :FCL
        ISO :4510
        Gross Wt. :31.81 MT
    SEAL 1 : 0M0130728
    Vsl : ALEXANDRA MAERSK
    VIA :S@335 junk CONTAINER SCAN
    Trk No : MH43BX1488
    Driver : BABALU KUMAR pe Sat
    """
    fields = extract_eir_fields(text)
    assert fields["ContainerNo"].value == "MRKU5014206"
    assert fields["BATNo"].value == "D391"
    assert fields["TransID"].value == "5599372"
    assert fields["LICNo"].value == "MH43BX1488"
    assert fields["Via"].value == "S0335"
    assert fields["Vessel"].value == "ALEXANDRA MAERSK"
    assert fields["ISOCode"].value == "4510"
    assert fields["SealNo1"].value.startswith("OM")
    assert fields["Driver"].value == "BABALU KUMAR"


def test_extract_psa_bat_and_tofrom():
    text = """
    PSA MUMBAI BMCT-EIR
    EIR NO: 4339869
    LIC NO: MH43BX1488
    Container NO: MSMU1908508
    Vessel/VIA: SAV/SO696
    To/From: CLP CFS
    CO/UN NO:
    No: B723
    No1: C//EU31716082
    No2: N//NOSEAL
    """
    fields = extract_eir_fields(text)
    assert fields["BATNo"].value == "B723"
    assert fields["ToFrom"].value == "CLP CFS"
    assert fields["SealNo1"].value == "EU31716082"
    assert fields["VesselVia"].value == "SAV/S0696"


def test_reject_false_plate_jo5p7():
    text = "VIA :S@335 <1 jo5p7, CONTAINER SCAN\nCTR No :NYKU4768188"
    fields = extract_eir_fields(text)
    assert "LICNo" not in fields
    assert fields["ContainerNo"].value == "NYKU4768188"


def test_extract_regex_fallback_without_labels():
    text = "sighting MSMU1908508 on truck MH43BX1488 near gate"
    fields = extract_eir_fields(text)
    assert fields["ContainerNo"].value == "MSMU1908508"
    assert fields["LICNo"].value == "MH43BX1488"


def test_blank_noseal_dropped():
    text = "Seal No: NOSEAL\nContainer No: NYKU4768188"
    fields = extract_eir_fields(text)
    assert "SealNo1" not in fields
    assert fields["ContainerNo"].value == "NYKU4768188"


def test_extract_psa_category():
    text = """
    PSA MUMBAI BMCT-EIR
    Cateqorny : IMPORT
    EIR No: 4339869
    LIC NO: MH43BX1488
    Container NO: MSMU1908508
    User/Login ID: BPMO0G
    """
    fields = extract_eir_fields(text)
    assert fields["Category"].value == "IMPORT"
    assert fields["UserLoginID"].value == "BPM006"


def test_extract_gateway_flags_and_client():
    text = """
    GATEWAY TERMINALS INDIA PVT.LTD
    EIR-Deliver Import
    CTR No :MRKUS@14206
    Client Code :MGl
    Is Reefer :NO
    Is ODC :NO
    Is Damage :NO
    Scan : SCANNED CLEAN
    Trk No : MH43BX1488
    """
    fields = extract_eir_fields(text)
    assert fields["ClientCode"].value == "MG1"
    assert fields["IsReefer"].value == "NO"
    assert fields["IsODC"].value == "NO"
    assert fields["IsDamage"].value == "NO"
    assert "SCANNED" in fields["Scan"].value


def test_extract_dpworld_noisy():
    text = """
    DP World Nhava Sheva ICT
    BAT (jE 56
    Lac Stir Pickup Laden
    iSO Code 153
    Group Code CFSZNI
    Truck WIH4GAF 43 75 :
    Status Full
    Yard position - ALVY
    """
    fields = extract_eir_fields(text)
    assert fields["LICNo"].value == "MH46AF4375"
    assert fields["BATNo"].value == "UE56"
    assert fields["ISOCode"].value == "4532"
    assert fields["LocSlip"].value == "Pickup Laden"
    assert fields["YardPosition"].value == "4L10"
    assert fields["GroupCode"].value == "CFSZNT"


def test_field_order_matches_slip_layout():
    from eir_ocr.extract import flat_values

    psa = extract_eir_fields(
        """
        PSA Mumbai BMCT
        Category: IMPORT
        Shipping Agent: MSC
        EIR No: 4339869
        LIC No: MH43BX1488
        Container No: MSMU1908508
        """
    )
    keys = list(flat_values(psa))
    assert keys.index("Category") < keys.index("ShippingAgent") < keys.index("EIRNo")
    assert keys.index("LICNo") < keys.index("ContainerNo")

    dp = extract_eir_fields(
        """
        DP World Nhava Sheva ICT
        BAT (jE 56
        Lac Stir Pickup Laden
        iSO Code 153
        Group Code CFSZNI
        Truck WIH4GAF 43 75 :
        Status Full
        Yard position - ALVY
        """
    )
    keys = list(flat_values(dp))
    assert keys.index("BATNo") < keys.index("LocSlip") < keys.index("ISOCode")
    assert keys.index("LICNo") < keys.index("YardPosition")

    gw = extract_eir_fields(
        """
        Gateway Terminals India Pvt Ltd
        CTR No :MRKU5014206
        BAT ID: D391
        Client Code :MG1
        Is Reefer :NO
        Trk No : MH43BX1488
        """
    )
    keys = list(flat_values(gw))
    assert keys.index("ContainerNo") < keys.index("BATNo") < keys.index("ClientCode")
    assert keys.index("IsReefer") < keys.index("LICNo")


def test_unknown_labels_go_to_extras():
    """Future slip fields not in EIR_FIELDS are preserved under extras."""
    from eir_ocr.extract import extract_eir_bundle

    text = """
    PSA Mumbai BMCT
    Category: IMPORT
    EIR No: 4339869
    LIC No: MH43BX1488
    Container No: MSMU1908508
    IMCO/UN NO: 1234
    Emergency ContNo: 9876543210
    """
    bundle = extract_eir_bundle(text)
    assert bundle.fields["Category"].value == "IMPORT"
    assert bundle.fields["ContainerNo"].value == "MSMU1908508"
    assert "ImcoUnNo" in bundle.extras
    assert bundle.extras["ImcoUnNo"].value == "1234"
    assert "EmergencyContno" in bundle.extras
    assert bundle.extras["EmergencyContno"].value == "9876543210"


def test_tesseract_optional_smoke():
    from eir_ocr.engine import tesseract_status

    ready, ver = tesseract_status()
    if not ready:
        pytest.skip(f"tesseract unavailable: {ver}")
    assert ver
