import os
import tempfile
import zipfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from mealie.schema.recipe.recipe_ingredient import RecipeIngredientBase

from ._migration_base import BaseMigrator
from .utils.migration_helpers import import_image


def _format_time(minutes: int) -> str:
    """Formats time from minutes to a human-readable string."""
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}")
    return " ".join(parts)


def convert_to_float(value):
    try:
        value = value.strip()  # Remove any surrounding spaces
        if " " in value:  # Check for mixed fractions like "1 1/2"
            # Split into whole number and fraction
            whole, fraction = value.split(" ", 1)
            return float(whole) + float(Fraction(fraction))
        return float(Fraction(value))  # Convert fraction or whole number
    except (ValueError, ZeroDivisionError):
        return None  # Return None for invalid values


def extract_instructions(instructions: str) -> list[str]:
    """Splits the instruction text into steps."""
    return instructions.split("\n") if instructions else []


# def extract_ingredients(ingredient_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
#     """Extracts ingredient details from parsed Cook'n ingredient data."""
#     ingredients = []
#     for ingredient in ingredient_data:
#         base_ingredient = RecipeIngredientBase(
#             quantity=ingredient.get("AMOUNT_QTY", "1"),
#             unit=ingredient.get("AMOUNT_UNIT"),
#             food=ingredient.get("INGREDIENT_FOOD_ID"),
#         )
#         ingredients.append({"title": None, "note": base_ingredient.display})
#     return ingredients


class DSVParser:
    def __init__(self, directory: Path):
        self.directory = directory
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.load_files()

    def load_files(self):
        """Loads all .dsv files from the directory into lists of dictionaries."""
        for file in self.directory.glob("*.dsv"):
            with open(file, "rb") as f:
                file_contents = f.read().decode("utf-8", errors="ignore")

            # Replace unique delimiters
            file_contents = file_contents.replace("||||", "\x06")
            file_contents = file_contents.replace("!@#%^&*()", "\x07")

            # Manually parse rows
            rows = file_contents.strip().split("\x07")
            if not rows:
                continue  # Skip empty files

            # Extract header
            headers = rows[0].split("\x06")
            data = [dict(zip(headers, row.split("\x06"), strict=False)) for row in rows[1:] if row]

            self.tables[file.stem] = data  # Store parsed table

    def query_by_id(self, table_name: str, column_name: str, ids: list, return_first_only=False):
        """Returns rows from a specified table where column_name matches any of the provided IDs."""
        if table_name not in self.tables:
            raise ValueError(f"Table '{table_name}' not found.")

        results = [row for row in self.tables[table_name] if row.get(column_name) in ids]

        if len(results) == 0:
            results.append({})

        if return_first_only and results:
            return results[0]
        return results

    def get_table(self, table_name: str):
        """Returns the entire table as a list of dictionaries."""
        if table_name not in self.tables:
            raise ValueError(f"Table '{table_name}' not found.")
        return self.tables[table_name]

    def list_tables(self):
        """Returns a list of available tables."""
        return list(self.tables.keys())


class CooknMigrator(BaseMigrator):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.name = "cookn"
        self.key_aliases = []

    def _process_recipe_document(self, _recipe_row, db) -> dict:
        recipe_data = {}

        # Select db values
        _recipe_id = _recipe_row["ID"]
        _recipe_desc_row = db.query_by_id("temp_recipe_desc", "ID", [_recipe_id], return_first_only=True)
        _chapter_id = _recipe_desc_row["PARENT"]
        _chapter_row = db.query_by_id("temp_chapter_desc", "ID", [_chapter_id], return_first_only=True)
        _cookbook_id = _chapter_row["PARENT"]
        _cookbook_row = db.query_by_id("temp_cookBook_desc", "ID", [_cookbook_id], return_first_only=True)
        _media_row = db.query_by_id("temp_media", "ENTITY_ID", [_recipe_id], return_first_only=True)
        _media_id = _media_row.get("ID", "")

        # Parse general recipe info
        cookbook = _cookbook_row.get("TITLE", "")
        chapter = _chapter_row.get("TITLE", "")
        name = _recipe_desc_row.get("TITLE", "")
        description = _recipe_desc_row.get("DESCRIPTION", "")
        serves = _recipe_row["SERVES"]
        prep_time = int(_recipe_row["PREPTIME"])
        cook_time = int(_recipe_row["COOKTIME"])

        recipe_data["recipeCategory"] = [cookbook]
        recipe_data["tags"] = [chapter]
        recipe_data["name"] = name
        recipe_data["description"] = description
        recipe_data["recipeYield"] = serves
        recipe_data["prepTime"] = _format_time(prep_time)
        recipe_data["performTime"] = _format_time(cook_time)
        recipe_data["totalTime"] = _format_time(prep_time + cook_time)

        # Parse and rename image

        if _media_id != "":
            _media_type = _media_row["MEDIA_CONTENT_TYPE"]
            # Determine file extension based on media type
            _extension = _media_type.split("/")[-1]
            _old_image_path = os.path.join(db.directory, str(_media_id))
            new_image_path = f"{_old_image_path}.{_extension}"
            # Rename the file if it exists and has no extension
            if os.path.exists(_old_image_path) and not os.path.exists(new_image_path):
                os.rename(_old_image_path, new_image_path)
            if Path(new_image_path).exists():
                recipe_data["image"] = [new_image_path]

        # Parse ingrediants
        ingredients = []
        _ingrediant_rows = db.query_by_id("temp_ingredient", "PARENT_ID", [_recipe_id])
        for _ingrediant_row in _ingrediant_rows:
            _unit_id = _ingrediant_row.get("AMOUNT_UNIT", "")
            _unit_row = db.query_by_id("temp_unit", "ID", [_unit_id], return_first_only=True)
            _food_id = _ingrediant_row.get("INGREDIENT_FOOD_ID", "")
            _food_row = db.query_by_id("temp_food", "ID", [_food_id], return_first_only=True)
            _brand_id = _ingrediant_row.get("BRAND_ID", "")
            _brand_row = db.query_by_id("temp_brand", "ID", [_brand_id], return_first_only=True)

            amount = convert_to_float(_ingrediant_row.get("AMOUNT_QTY_STRING", "1"))
            unit = _unit_row.get("ABBREVIATION")

            food_name_singluar = _food_row.get("NAME", "")
            food_name_plural = _food_row.get("PLURAL_NAME", "")
            if food_name_singluar != "" and food_name_plural != "":
                if unit is None:
                    if amount is not None and amount > 1:
                        food_name = _food_row.get("PLURAL_NAME", "")
                    else:
                        food_name = _food_row.get("NAME", "")
                else:
                    food_name = _food_row.get("NAME", "")
            else:
                if food_name_singluar != "":
                    food_name = food_name_singluar
                else:
                    food_name = food_name_plural

            if unit is None:
                unit = ""
            else:
                if amount is not None and amount > 1:
                    unit = _unit_row.get("PLURAL_NAME")
                else:
                    unit = _unit_row.get("NAME")

            pre_qualifier = _ingrediant_row.get("PRE_QUALIFIER", "")
            if pre_qualifier == "[null]":
                pre_qualifier = ""
            post_qualifier = _ingrediant_row.get("POST_QUALIFIER", "")
            if post_qualifier == "[null]":
                post_qualifier = ""
            brand = _brand_row.get("NAME", "")
            if brand == "[null]":
                brand = ""

            base_ingredient = RecipeIngredientBase(
                quantity=amount,
                unit=unit,
                food=pre_qualifier + " " + food_name + " " + post_qualifier + " " + brand,
                notes=None,
            )
            _display_order = int(_ingrediant_row.get("DISPLAY_ORDER", ""))
            ingredients.append({"title": None, "order": _display_order, "note": base_ingredient.display})
        ingredients = sorted(ingredients, key=lambda d: d["order"])
        recipe_data["recipeIngredient"] = ingredients

        # Parse instructions
        recipe_data["recipeInstructions"] = extract_instructions(_recipe_row["INSTRUCTIONS"])

        return recipe_data

    def _migrate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(self.archive) as zip_file:
                zip_file.extractall(tmpdir)

            source_dir = self.get_zip_base_path(Path(tmpdir))
            db = DSVParser(source_dir)
            _recipe_table = db.get_table("temp_recipe")
            recipes_as_dicts = [self._process_recipe_document(_recipe_row, db) for _recipe_row in _recipe_table]

            recipes = [self.clean_recipe_dictionary(x) for x in recipes_as_dicts]
            results = self.import_recipes_to_database(recipes)
            recipe_lookup = {r.slug: r for r in recipes}
            for slug, recipe_id, status in results:
                if status:
                    r = recipe_lookup.get(slug)
                    if r and r.image:
                        import_image(r.image, recipe_id)
