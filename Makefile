.PHONY: demo seed harvest counts score sample test report resweep ui ui-local ui-install ui-test ui-build daily daily-dry schedule-install apply-queue autoapply schedule-install-autoapply watch watch-dry schedule-install-watch schedule-remove-watch clean-pyc

# Never let stale bytecode outlive its source. A timestamp+size-validated .pyc
# survives an in-place edit that changes neither (e.g. swapping SCHEMA_VERSION
# 3 -> 4), so the import serves the old constant while the file reads the new
# one — hours lost to that on 2026-08-02. Writing none is the durable fix;
# clean-pyc clears any already on disk.
export PYTHONDONTWRITEBYTECODE := 1

seed:
	uv run python -m jobhunt seed

harvest:
	uv run python -m jobhunt harvest

counts:
	uv run python -m jobhunt counts

score:
	uv run python -m jobhunt score

sample:
	uv run python -m jobhunt sample

test: clean-pyc
	uv run pytest -q

clean-pyc:
	@find . \( -path ./.venv -o -path '*/node_modules' \) -prune -o \
		-name '__pycache__' -type d -print0 | xargs -0 rm -rf

report:
	uv run python -m jobhunt report

resweep:
	uv run python resweep.py

ui:
	uv run uvicorn jobhunt.web.app:app --host 127.0.0.1 --port 8787

# One-command synthetic demo: no profile, network, LLM, or keys. Never touches data/jobhunt.db.
demo:
	@test -f jobhunt/web/static/spa/index.html || $(MAKE) ui-build
	uv run python -m jobhunt demo --db data/demo.db --reset
	JOBHUNT_DEMO=1 JOBHUNT_DB_PATH=data/demo.db uv run uvicorn jobhunt.web.app:app --host 127.0.0.1 --port 8787

ui-local:
	JOBHUNT_LLM_BASE_URL=http://localhost:11434/v1 \
	JOBHUNT_LLM_MODEL=qwen2.5:7b-instruct-64k \
	uv run uvicorn jobhunt.web.app:app --host 127.0.0.1 --port 8787

ui-install:
	npm --prefix jobhunt/web/frontend install

ui-test:
	npm --prefix jobhunt/web/frontend test

jobhunt/web/frontend/node_modules: jobhunt/web/frontend/package-lock.json
	npm --prefix jobhunt/web/frontend install
	@touch jobhunt/web/frontend/node_modules

ui-build: jobhunt/web/frontend/node_modules
	npm --prefix jobhunt/web/frontend run build

daily:
	uv run python daily.py

daily-dry:
	uv run python daily.py --no-harvest

schedule-install:
	@echo "Installing com.jobhunt.daily.plist -> ~/Library/LaunchAgents (daily 08:00 'make daily' run)."
	@echo "launchd's env deliberately lacks SPEND_OK/OPENROUTER_API_KEY, so scheduled runs never spend."
	scripts/install-launchd.sh daily >/dev/null
	launchctl load ~/Library/LaunchAgents/com.jobhunt.daily.plist

apply-queue:
	uv run python -m jobhunt apply-queue

autoapply:
	uv run python -m jobhunt autoapply run

schedule-install-autoapply:
	@echo "Installing com.jobhunt.autoapply.plist -> ~/Library/LaunchAgents (daily 19:00 'make autoapply' run)."
	@echo "This job SUBMITS applications on its own (Greenhouse/Lever/Ashby, 100/day cap). It installs only"
	@echo "once 'autoapply status --require-armed' passes: first 10 dry runs reviewed, then 'autoapply arm'."
	@echo "Stop it any time with 'autoapply disarm' or by creating data/autoapply/KILL."
	@echo "Its launchd env lacks SPEND_OK/OPENROUTER_API_KEY: tailoring runs on the claude CLI, never metered credits."
	@# the arm check runs first: if it fails, make stops before anything is copied or loaded.
	uv run python -m jobhunt autoapply status --require-armed
	scripts/install-launchd.sh autoapply >/dev/null
	@# unload first: launchctl load on an already-loaded job fails with
	@# "Input/output error" and leaves launchd running the OLD definition, so a
	@# reinstall silently does nothing. Ignore unload errors on a first install.
	-launchctl unload ~/Library/LaunchAgents/com.jobhunt.autoapply.plist 2>/dev/null
	launchctl load ~/Library/LaunchAgents/com.jobhunt.autoapply.plist

watch:
	uv run python watch.py

watch-dry:
	uv run python watch.py --no-harvest --print

schedule-install-watch:
	@echo "Installing com.jobhunt.watch.plist -> ~/Library/LaunchAgents (every 30 min 'make watch': gh/lever/ashby sweep, score new, ntfy push)."
	@echo "Push target: data/ntfy_topic.local (one line) or JOBHUNT_NTFY_TOPIC. Without one the tick logs instead of pushing."
	scripts/install-launchd.sh watch >/dev/null
	-launchctl unload ~/Library/LaunchAgents/com.jobhunt.watch.plist 2>/dev/null
	launchctl load ~/Library/LaunchAgents/com.jobhunt.watch.plist

schedule-remove-watch:
	-launchctl unload ~/Library/LaunchAgents/com.jobhunt.watch.plist 2>/dev/null
	rm -f ~/Library/LaunchAgents/com.jobhunt.watch.plist
