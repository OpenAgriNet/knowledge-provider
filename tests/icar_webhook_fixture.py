"""Sanitized ICAR payload exercised by canonical intake tests."""

import copy

OVERVIEW = (
    "Timely harvesting of AKJ-1 helps preserve grain appearance, processing quality and "
    "suitability for flaking. Proper drying and handling after harvest are essential to "
    "minimize grain damage and deterioration."
)
SYMPTOMS = (
    "Panicles become mature and dry, grains become hard and attain characteristic colour, "
    "and most leaves begin to dry."
)
CONDITIONS = (
    "Crop has reached physiological maturity and panicles are fully developed; grains are "
    "firm and suitable for flaking quality."
)
ADVISORY = (
    "Harvest AKJ-1 at physiological maturity when the grains are fully developed and firm. "
    "Timely harvesting is important for maintaining grain quality required for flaking. "
    "Avoid delayed harvesting, which may increase grain weathering, shattering and mould "
    "risk. After harvesting, dry the panicles properly, thresh carefully, clean the grain "
    "and reduce moisture to a safe level before storage or processing."
)

SORGHUM_PAYLOAD = {
    "contentID": 987,
    "title": "Sorghum- Harvesting Management",
    "commodityName": "Sorghum",
    "creatorName": "jak",
    "domainName": "Agriculture",
    "subdomainName": "Cereals",
    "instituteName": "ICAR–Directorate of Knowledge Management in Agriculture (DKMA)",
    "isPublished": 1,
    "status": "Published",
    "popList": [
        {
            "popID": 819,
            "season": "Rabi",
            "problemType": '"Harvesting Management"',
            "problemDetails": '"Method Of Harvesting"',
            "startPeriod": "105",
            "endPeriod": "120",
            "status": "Published",
            "description": f'"{OVERVIEW}"',
            "applicableSymptoms": [SYMPTOMS],
            "applicableCondition": [CONDITIONS],
            "advisory": f'"{ADVISORY}"',
        }
    ],
}


def bulk_payload(published_count=205):
    payload = copy.deepcopy(SORGHUM_PAYLOAD)
    template = payload["popList"][0]
    payload["popList"] = [
        {
            **copy.deepcopy(template),
            "popID": 10000 + index,
            "problemDetails": f"Harvesting reference {index}",
            "description": f"Published overview {index}",
        }
        for index in range(published_count)
    ]
    payload["popList"].insert(100, {
        **copy.deepcopy(template), "popID": 90001, "status": "Draft",
        "advisory": "WITHHELD-DRAFT-MARKER",
    })
    payload["popList"].append({
        **copy.deepcopy(template), "popID": 90002, "status": "Withheld",
        "advisory": "WITHHELD-ENTRY-MARKER",
    })
    return payload
