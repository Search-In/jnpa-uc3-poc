"""COARRI/COPRAR parser tests — fixtures mirror the LIVE dt.jnpa.in corpus
(shapes captured 2026-08-06 from the edi-messages group)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.edi_vessel.parsers import (  # noqa: E402
    EdiVesselParseError,
    detect_doc_type,
    direction_from_filename,
    parse_document,
    parse_ist_datetime,
)

COARRI_XML = """<ContLoadingNDischargeOder><DocumentHeader><DocumentReference>
<DocumentType>COARRI</DocumentType><DocumentName>Container Loading-Discharge Report</DocumentName>
<DocumentNumber>2296802</DocumentNumber><CommonRefNumber>BMC05082026230903</CommonRefNumber>
<MessageType>9</MessageType><SenderID>bmctpld001</SenderID></DocumentReference>
</DocumentHeader><DocumentDetails><COARRIHeader><VCN>INNSA1BM0S1139</VCN><IMONumber></IMONumber>
<CallSign></CallSign><TOCode>INNSA1BMC1</TOCode><VesselAgentCode>MIL</VesselAgentCode>
<VesselFlag>PA</VesselFlag><NoOfContainer>2</NoOfContainer></COARRIHeader><COARRIDetails>
<COARRIItem><EquipmentStatusCode>FCL</EquipmentStatusCode><ContainerNumber>SAJU3136840</ContainerNumber>
<CustomsContSealNumber>798105</CustomsContSealNumber><ShipperContainerSealNumber>ES902668491</ShipperContainerSealNumber>
<CACode>MIL</CACode><ContLineCode>MIL</ContLineCode><ContISOCode>45G1</ContISOCode>
<ContClasssificationCode>GP</ContClasssificationCode><ICDIndicator>N</ICDIndicator>
<CShippingDateTime>05082026:08:47</CShippingDateTime><CLandDateTime>05082026:08:47</CLandDateTime>
<ContainerDamageIndicator>No</ContainerDamageIndicator><ContainerDamageDesc>NONE</ContainerDamageDesc>
<BerthingDateTime>05082026:07:30</BerthingDateTime><SealStatus>Yes</SealStatus></COARRIItem>
<COARRIItem><EquipmentStatusCode>FCL</EquipmentStatusCode><ContainerNumber>CAIU2807800</ContainerNumber>
<CACode>MIL</CACode><ContLineCode>MIL</ContLineCode><ContISOCode>22G1</ContISOCode></COARRIItem>
</COARRIDetails></DocumentDetails></ContLoadingNDischargeOder>"""

COPRAR_XML = """<AdvContainerList><DocumentHeader><DocumentReference>
<DocumentType>COPRAR</DocumentType><DocumentName>ADVANCED CONTAINER LIST</DocumentName>
<DocumentNumber>30096807000002</DocumentNumber><CommonRefNumber>2026071660019680</CommonRefNumber>
<MessageType>9</MessageType><SenderID>dmapld001</SenderID></DocumentReference></DocumentHeader>
<DocumentDetails><COPRARHeader><VCN>INNSA1BM0S1194</VCN><IMONumber></IMONumber><CallSign></CallSign>
<VoyageNumber></VoyageNumber><TOOrDockCode>INNSA1BMC1</TOOrDockCode><FlagCountryCode>PA</FlagCountryCode>
<SACode>DMA</SACode><RotationNumber>1198358</RotationNumber><RotationNumberDate>04082026</RotationNumberDate>
<TotNoContainer>1</TotNoContainer></COPRARHeader><COPRARDetailsSummary><COPRARItem>
<EquipmentStatusCode>1</EquipmentStatusCode><ContainerNumber>SEGU4633897</ContainerNumber>
<ContainerStatus>8</ContainerStatus><ContainerISOCode>4532</ContainerISOCode>
<ContainerTareWeight>4.65</ContainerTareWeight><ContainerGrossWeight>17.63</ContainerGrossWeight>
<PortOfOrigin>INNSA1</PortOfOrigin><PortOfLoading>INNSA1</PortOfLoading><IGMLineNumber>0</IGMLineNumber>
<IGMSubLineNumber>0</IGMSubLineNumber><CargoType>9</CargoType><CACode>DMA</CACode><IMOClass>9</IMOClass>
<PortOfDischarge>INHZA1</PortOfDischarge><FinalPortOfDischarge>INHZA1</FinalPortOfDischarge>
<DisposalMode>1</DisposalMode></COPRARItem></COPRARDetailsSummary></DocumentDetails></AdvContainerList>"""


def test_detect_and_direction():
    assert detect_doc_type(COARRI_XML) == "COARRI"
    assert detect_doc_type(COPRAR_XML) == "COPRAR"
    assert detect_doc_type("<Other/>") is None
    assert direction_from_filename("COARRI_LOAD_05082026230900.xml") == "LOAD"
    assert direction_from_filename("COPRAR_DISCH_1.xml") == "DISCHARGE"
    assert direction_from_filename("whatever.xml") is None


def test_parse_coarri_live_shape():
    doc_type, header, rows = parse_document(COARRI_XML)
    assert doc_type == "COARRI"
    assert header["document_number"] == "2296802"
    assert header["vcn"] == "INNSA1BM0S1139"
    assert header["terminal_code"] == "INNSA1BMC1"
    assert header["agent_code"] == "MIL"
    assert header["declared_count"] == 2
    assert len(rows) == 2
    first = rows[0]
    assert first["container_no"] == "SAJU3136840"
    assert first["iso_code"] == "45G1"
    assert first["line_code"] == "MIL"
    assert first["seal_no"] == "798105"
    assert first["icd_indicator"] is False
    assert first["damage_indicator"] is False
    ts = first["shipping_ts"]
    assert (ts.year, ts.month, ts.day, ts.hour, ts.minute) == (2026, 8, 5, 8, 47)
    assert ts.utcoffset().total_seconds() == 5.5 * 3600  # IST
    # Unknown-but-present elements ride in extra, not dropped.
    assert first["extra"]["SealStatus"] == "Yes"
    assert first["extra"]["ContClasssificationCode"] == "GP"


def test_parse_coprar_live_shape():
    doc_type, header, rows = parse_document(COPRAR_XML)
    assert doc_type == "COPRAR"
    assert header["vcn"] == "INNSA1BM0S1194"
    assert header["terminal_code"] == "INNSA1BMC1"
    assert header["agent_code"] == "DMA"
    assert header["rotation_no"] == "1198358"
    assert str(header["rotation_date"]) == "2026-08-04"
    (row,) = rows
    assert row["container_no"] == "SEGU4633897"
    assert row["gross_weight"] == 17.63
    assert row["tare_weight"] == 4.65
    assert row["pol"] == "INNSA1"
    assert row["pod"] == "INHZA1"
    assert row["final_pod"] == "INHZA1"
    assert row["igm_line"] == 0
    assert row["extra"]["DisposalMode"] == "1"


COPARN_XML = """<ContainerRelease><DocumentHeader><DocumentReference>
<DocumentType>COPARN</DocumentType><DocumentName>Empty Container Release Order</DocumentName>
<DocumentNumber>12072026100000</DocumentNumber><CommonRefNumber>2026071259400968</CommonRefNumber>
<MessageType>9</MessageType><SenderID>ccansa001</SenderID></DocumentReference></DocumentHeader>
<DocumentDetails><COPARNHeader><VCN>INNSA1NF0S0977</VCN><VoyageNumber>0P50ZN1MA</VoyageNumber>
<SACode>CCA</SACode><LineCode>CCA</LineCode><TotNoContainer>1</TotNoContainer></COPARNHeader>
<ContainerDetails><Container><ContainerNumber>TGHU8682244</ContainerNumber>
<ContISOCode>4510</ContISOCode><EquipmentStatusCode>MTY</EquipmentStatusCode>
<ReleaseDateTime>07072026:03:55</ReleaseDateTime><PickupDateTime>07072026:08:33</PickupDateTime>
<DepotCode>ECD-NSA-01</DepotCode><ReExportBondPCNo></ReExportBondPCNo></Container>
</ContainerDetails></DocumentDetails></ContainerRelease>"""


def test_parse_coparn_live_shape():
    doc_type, header, rows = parse_document(COPARN_XML)
    assert doc_type == "COPARN"
    assert detect_doc_type(COPARN_XML) == "COPARN"
    assert header["vcn"] == "INNSA1NF0S0977"
    assert header["voyage"] == "0P50ZN1MA"
    assert header["line_code"] == "CCA"
    assert header["agent_code"] == "CCA"
    assert header["declared_count"] == 1
    (row,) = rows
    assert row["container_no"] == "TGHU8682244"
    assert row["equipment_status"] == "MTY"
    assert row["depot_code"] == "ECD-NSA-01"
    rel, pick = row["release_ts"], row["pickup_ts"]
    assert (rel.day, rel.hour, rel.minute) == (7, 3, 55)
    assert (pick.day, pick.hour, pick.minute) == (7, 8, 33)
    assert row["iso_valid"] is True


def test_row_without_container_is_skipped_and_bad_xml_rejected():
    xml = COARRI_XML.replace("SAJU3136840", "")
    _, _, rows = parse_document(xml)
    assert [r["container_no"] for r in rows] == ["CAIU2807800"]
    with pytest.raises(EdiVesselParseError):
        parse_document("<ContLoadingNDischargeOder><broken")
    with pytest.raises(EdiVesselParseError):
        parse_document("<SomethingElse/>")


def test_ist_datetime_edge():
    assert parse_ist_datetime("31122026:23:59").day == 31
    assert parse_ist_datetime("bogus") is None
    assert parse_ist_datetime(None) is None
