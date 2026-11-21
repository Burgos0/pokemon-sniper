import unittest

from sniper import metadata

# Trimmed from the live TCGCSV products response for group 1494 (2026-11-20).
GENGAR = {
    "productId": 94168, "name": "M Gengar EX", "cleanName": "M Gengar EX", "groupId": 1494,
    "url": "https://www.tcgplayer.com/product/94168/pokemon-xy-phantom-forces-m-gengar-ex",
    "extendedData": [
        {"name": "Number", "displayName": "Card Number", "value": "35/119"},
        {"name": "Rarity", "displayName": "Rarity", "value": "Ultra Rare"},
        {"name": "Card Type", "displayName": "Card Type", "value": "Psychic"},
    ],
}
BOOSTER_BOX = {"productId": 1, "name": "XY Phantom Forces Booster Box", "groupId": 1494,
               "extendedData": []}
CODE_CARD = {"productId": 2, "name": "Code Card - Collector Chest", "groupId": 1494}
NO_NUMBER_WITH_RARITY = {"productId": 3, "name": "Odd Product", "groupId": 1494,
                         "extendedData": [{"name": "Rarity", "value": "Promo"},
                                          {"name": "Number", "value": "  "}]}


class SealedDetectionTests(unittest.TestCase):
    def test_single_with_card_number_is_not_sealed(self):
        record = metadata.parse_product(GENGAR, "t")
        self.assertEqual(record["is_sealed"], 0)
        self.assertEqual(record["card_number"], "35/119")
        self.assertEqual(record["rarity"], "Ultra Rare")
        self.assertEqual(record["name"], "M Gengar EX")

    def test_products_without_card_number_are_sealed(self):
        for product in (BOOSTER_BOX, CODE_CARD, NO_NUMBER_WITH_RARITY):
            self.assertEqual(metadata.parse_product(product, "t")["is_sealed"], 1, product["name"])

    def test_group_release_date_comes_from_published_on(self):
        group = {"groupId": 1494, "name": "XY - Phantom Forces", "abbreviation": "PHF",
                 "isSupplemental": False, "publishedOn": "2014-11-05T00:00:00",
                 "modifiedOn": "2026-10-26T13:54:54.053"}
        record = metadata.parse_group(group, "t")
        self.assertEqual(record["release_date"], "2014-11-05")
        self.assertEqual(record["is_supplemental"], 0)


if __name__ == "__main__":
    unittest.main()
