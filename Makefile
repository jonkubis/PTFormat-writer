PYTHON ?= python3

.PHONY: all test clean

all:
	$(PYTHON) -m compileall -q ptxformatwriter

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

clean:
	find ptxformatwriter tests -type d -name __pycache__ -prune -exec rm -rf {} +
