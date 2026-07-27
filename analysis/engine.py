import re

class Engine:
    CONTRACT_PATTERN = r"([FGHJKMNQUVXZ])(\d{2})"

    def get_required_codes(self, expression, seasons):
        """
        Example: if expr="Z26-F27" and seasons=[2023, 2024]
        Returns: ["Z23", "F24", "Z24", "F25"]
        """
        matches = re.findall(self.CONTRACT_PATTERN, expression)
        if not matches: return []
        
        ref_year_in_expr = int(matches[0][1])
        codes = set()

        for s in seasons:
            for month, yy in matches:
                offset = int(yy) - ref_year_in_expr
                # Calculate the specific contract year for this season
                contract_yy = (s + offset) % 100
                codes.add(f"{month}{contract_yy:02d}")
        
        return list(codes)