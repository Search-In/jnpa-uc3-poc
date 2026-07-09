from datetime import datetime, timezone


def demo_balance(rc_number: str) -> dict:
    return {
        "status": "success",
        "data": {
            "rc_number": rc_number,
            "tag_id": "DEMOFASTAG001",
            "provider_name": "DEMO_BANK",
            "provider_code": "DEMO001",
            "customer_name": "DEMO_USER",
            "available_recharge_limit": "5000.00",
            "available_balance": "850.00",
            "tag_status": "ACTIVE",
            "vehicle_class": "4",
            "vehicle_class_desc": "Car / Jeep / Van",
            "model_name": "DEMO_VEHICLE",
        },
    }


def demo_transactions(rc_number: str) -> dict:
    return {
        "status": "success",
        "data": [
            {
                "seq_no": "DEMO-001",
                "rc_number": rc_number,
                "transaction_date_time": datetime.now(timezone.utc).isoformat(),
                "toll_plaza_name": "JNPA Demo Toll Plaza",
                "toll_plaza_geocode": "18.95,72.95",
                "vehicle_type": "CAR",
                "lane_direction": "ENTRY",
                "bank_name": "DEMO_BANK",
                "status": "SUCCESS",
            }
        ],
    }


def demo_toll_enroute(payload: dict | None = None) -> dict:
    """Deterministic demo Toll-Enroute response (no external ULIP dependency).

    Echoes the request's source/destination/vehicle so the demo reads coherently,
    and returns a small vendor-shaped plaza list. Vendor camelCase aliases are
    used so ``map_toll_enroute`` validates it exactly like a real ULIP payload.
    """
    p = payload or {}
    return {
        "status": "success",
        "data": {
            "clientId": p.get("clientId"),
            "sourceState": p.get("sourceState", "Maharashtra"),
            "sourceName": p.get("sourceName", "Nhava Sheva"),
            "destinationState": p.get("destinationState", "Maharashtra"),
            "destinationName": p.get("destinationName", "Pune"),
            "vehicleType": p.get("vehicleType", "TRUCK"),
            "duration": "3h 10m",
            "distance": "148.50",
            "toll_plaza_details": [
                {"name": "JNPA Demo Toll Plaza", "cost": "120.00", "lat": 18.95, "lng": 72.95},
                {"name": "Khalapur Demo Toll Plaza", "cost": "210.00", "lat": 18.82, "lng": 73.26},
            ],
        },
    }
