"""Parse TCGCSV groups/products into the `groups` and `products` tables.

Sealed detection (verified against live data on 2026-11-20, group 1494):
products carry an `extendedData` list of {name, displayName, value}. Singles
have an entry named "Number" (e.g. "35/119"); booster boxes, packs, ETBs,
blisters, collections and code cards do not. A product with no card Number
is treated as sealed. Rarity comes from the entry named "Rarity".
"""
from datetime import datetime


def extended_fields(product):
    return {
        entry.get("name"): entry.get("value")
        for entry in (product.get("extendedData") or [])
        if entry.get("name")
    }


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_product(product, fetched_at):
    fields = extended_fields(product)
    card_number = _clean(fields.get("Number"))
    return {
        "product_id": product["productId"],
        "group_id": product.get("groupId"),
        "name": product.get("name"),
        "clean_name": product.get("cleanName"),
        "card_number": card_number,
        "rarity": _clean(fields.get("Rarity")),
        "is_sealed": 0 if card_number else 1,
        "url": product.get("url"),
        "fetched_at": fetched_at,
    }


def parse_group(group, fetched_at):
    """Release date is `publishedOn` (the set's real release), never `modifiedOn`."""
    release_date = None
    published_on = group.get("publishedOn")
    if published_on:
        try:
            release_date = datetime.strptime(published_on[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            release_date = None
    return {
        "group_id": group["groupId"],
        "name": group.get("name"),
        "abbreviation": group.get("abbreviation"),
        "release_date": release_date,
        "is_supplemental": 1 if group.get("isSupplemental") else 0,
        "modified_on": group.get("modifiedOn"),
        "fetched_at": fetched_at,
    }
