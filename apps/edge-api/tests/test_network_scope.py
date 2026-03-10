from app.services.network_scope import parse_configured_subnets, parse_ipv4_route_subnets


def test_parse_configured_subnets_supports_multiple_separators():
    assert parse_configured_subnets("198.51.100.0/24, 10.0.0.0/24\n172.16.0.0/24;10.0.0.0/24") == [
        "198.51.100.0/24",
        "10.0.0.0/24",
        "172.16.0.0/24",
    ]


def test_parse_ipv4_route_subnets_ignores_default_and_loopback_routes():
    route_text = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
enp1s0\t00000000\t01BCA8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0
enp1s0\t006433C6\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
lo\t0000007F\t00000000\t0001\t0\t0\t0\t000000FF\t0\t0\t0
"""

    options = parse_ipv4_route_subnets(route_text)

    assert len(options) == 1
    assert options[0].cidr == "198.51.100.0/24"
    assert options[0].interface == "enp1s0"
