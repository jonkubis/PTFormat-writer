import unittest

from ptxformatwriter import PTFFormat


class CoreTests(unittest.TestCase):
    def test_scale_preserves_exact_values_when_rates_match(self):
        ptf = PTFFormat()
        ptf._targetrate = 44100
        ptf._sessionrate = 44100
        ptf.setrates()

        self.assertEqual(ptf._scale(158848200), 158848200)


if __name__ == "__main__":
    unittest.main()
