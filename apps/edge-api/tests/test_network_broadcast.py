from app.services.network_broadcast import (
    BroadcastAnnouncement,
    build_candidates_from_broadcast_announcements,
    parse_ssdp_response,
)


def test_parse_ssdp_response_extracts_headers():
    announcement = parse_ssdp_response(
        (
            b"HTTP/1.1 200 OK\r\n"
            b"ST: urn:schemas-upnp-org:device:Basic:1\r\n"
            b"USN: uuid:opendtu::urn:schemas-upnp-org:device:Basic:1\r\n"
            b"SERVER: OpenDTU/25.1 UPnP/1.1\r\n"
            b"LOCATION: http://198.51.100.84/description.xml\r\n\r\n"
        ),
        "198.51.100.84",
    )

    assert announcement is not None
    assert announcement.host == "198.51.100.84"
    assert announcement.server == "OpenDTU/25.1 UPnP/1.1"
    assert announcement.location == "http://198.51.100.84/description.xml"


def test_build_candidates_from_broadcast_announcements_groups_by_host():
    candidates = build_candidates_from_broadcast_announcements(
        [
            BroadcastAnnouncement(
                protocol="ssdp",
                host="198.51.100.84",
                service_type="urn:schemas-upnp-org:device:Basic:1",
                service_name="OpenDTU/25.1 UPnP/1.1",
                server="OpenDTU/25.1 UPnP/1.1",
                location="http://198.51.100.84/description.xml",
                usn="uuid:opendtu::urn:schemas-upnp-org:device:Basic:1",
                txt=[],
            ),
            BroadcastAnnouncement(
                protocol="mdns",
                host="198.51.100.84",
                service_type="_http._tcp.local",
                service_name="OpenDTU-OnBattery._http._tcp.local",
                server="opendtu.local",
                location="",
                usn="",
                txt=["path=/"],
            ),
        ]
    )

    assert len(candidates) == 1
    assert candidates[0].manufacturer == "OpenDTU"
    assert candidates[0].device_type == "pv_inverter"
    assert sorted(candidates[0].protocols) == ["mdns", "ssdp"]
    assert candidates[0].capabilities_hint["monitorable"] is False


def test_non_energy_broadcast_announcements_are_ignored():
    candidates = build_candidates_from_broadcast_announcements(
        [
            BroadcastAnnouncement(
                protocol="ssdp",
                host="198.51.100.10",
                service_type="urn:schemas-upnp-org:device:MediaServer:1",
                service_name="Generic Media Server",
                server="MiniDLNA/1.3.0",
                location="http://198.51.100.10/rootDesc.xml",
                usn="uuid:media::urn:schemas-upnp-org:device:MediaServer:1",
                txt=[],
            )
        ]
    )

    assert candidates == []


def test_fritzbox_gateway_announcements_are_ignored_despite_ipv6_tokens():
    candidates = build_candidates_from_broadcast_announcements(
        [
            BroadcastAnnouncement(
                protocol="ssdp",
                host="198.51.100.1",
                service_type="urn:schemas-upnp-org:service:WANIPv6FirewallControl:1",
                service_name="FRITZ!Box Fon WLAN 7390 UPnP/1.0 AVM FRITZ!Box Fon WLAN 7390 84.06.88",
                server="FRITZ!Box Fon WLAN 7390 UPnP/1.0 AVM FRITZ!Box Fon WLAN 7390 84.06.88",
                location="http://198.51.100.1:49000/igddesc.xml",
                usn="uuid:75802409-bccb-40e7-8e6a-c02506e49ec8::urn:schemas-upnp-org:service:WANIPv6FirewallControl:1",
                txt=[],
            )
        ]
    )

    assert candidates == []


def test_evcc_mdns_candidate_extracts_ipv4_identity_from_internal_url():
    candidates = build_candidates_from_broadcast_announcements(
        [
            BroadcastAnnouncement(
                protocol="mdns",
                host="fe80::921b:eff:fee4:d45f",
                service_type="_http._tcp.local",
                service_name="evcc._http._tcp.local",
                server="0.0.0.0.local",
                location="",
                usn="",
                txt=["path=/", "internal_url=http://198.51.100.158:7070"],
            )
        ]
    )

    assert len(candidates) == 1
    assert "network-host:198-51-100-158" in candidates[0].evidence["identity_keys"]
    assert "service-instance:evcc" in candidates[0].evidence["identity_keys"]
