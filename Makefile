IMAGE ?= blast-radius-entry
# "python3" does not exist on Windows; override with make PYTHON=python3 if needed.
PYTHON ?= python
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check map

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	$(PYTHON) score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make sample counts the misses.
check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	$(PYTHON) score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

# Traces sqlglot's tests at the pinned commit and writes the map into map/.
# Run once, then commit map/. Takes several minutes.
map:
	docker build --platform linux/amd64 --file tools/Dockerfile.map --tag blast-radius-map .
	docker run --rm --platform linux/amd64 --volume "$(CURDIR)/map:/out" blast-radius-map
