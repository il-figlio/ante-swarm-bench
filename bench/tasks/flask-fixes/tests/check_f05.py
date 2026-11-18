"""Check f05: README '## API' section documents DELETE /notes/<id> as
returning 200 with a JSON body, not 204."""
import re
import unittest


def _api_section(text):
    match = re.search(r"^## API[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return match.group(1) if match else ""


def _endpoint_block(section, method):
    idx = section.find(method)
    if idx == -1:
        return ""
    rest = section[idx:]
    nxt = re.search(r"\n###", rest)
    return rest[:nxt.start()] if nxt else rest


class TestDeleteDocs(unittest.TestCase):
    def setUp(self):
        with open("README.md") as fh:
            self.readme = fh.read()

    def test_api_section_exists(self):
        self.assertTrue(_api_section(self.readme),
                        "README.md has no '## API' section")

    def test_delete_endpoint_documented(self):
        block = _endpoint_block(_api_section(self.readme), "DELETE")
        self.assertTrue(block,
                        "no DELETE endpoint documented under '## API'")

    def test_delete_documents_200_json(self):
        block = _endpoint_block(_api_section(self.readme), "DELETE")
        self.assertTrue(block,
                        "no DELETE endpoint documented under '## API'")
        self.assertIn("200", block,
                      "DELETE docs should say it returns 200")
        self.assertNotIn("204", block,
                         "DELETE docs still claim 204; it returns 200 JSON")


if __name__ == "__main__":
    unittest.main()
