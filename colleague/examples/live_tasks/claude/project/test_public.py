"""Public contract checks. Run with python3 -B -m unittest -v test_public."""
import copy,json,unittest
from pathlib import Path
from solution import merge_windows

class PublicContract(unittest.TestCase):
    def test_cases(self):
        cases=json.loads(Path(__file__).with_name('public_cases.json').read_text())
        for case in cases:
            with self.subTest(case=case['id']):
                value=copy.deepcopy(case['input']);before=copy.deepcopy(value)
                if 'raises' in case:
                    with self.assertRaises({'TypeError':TypeError,'ValueError':ValueError}[case['raises']]):
                        merge_windows(value)
                else:self.assertEqual(merge_windows(value),case['expected'])
                self.assertEqual(value,before)
if __name__=='__main__':unittest.main()
