ARG BASE_IMAGE=debian:12-slim
FROM ${BASE_IMAGE}

# Verify the base package without compilers or vendor drivers.
RUN if command -v apt-get >/dev/null; then \
      apt-get update && apt-get install -y python3 python3-venv; \
    elif command -v dnf >/dev/null; then \
      dnf install -y python3 python3-pip; \
    else \
      apk add --no-cache python3 py3-pip; \
    fi
WORKDIR /project
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY tests ./tests
RUN python3 -m venv /tmp/sysmon && /tmp/sysmon/bin/pip install .
ENV PATH="/tmp/sysmon/bin:${PATH}"
USER 10001:10001
RUN python -m unittest discover -s tests -p 'test_*.py' && \
    sysmon --version && sysmon --check
