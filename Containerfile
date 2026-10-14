FROM registry.access.redhat.com/ubi9/python-312:latest

USER 0
# Pango is required by WeasyPrint for PDF output.
RUN dnf -y install pango && dnf clean all
USER 1001

WORKDIR /opt/app-root/src
COPY --chown=1001:0 pyproject.toml README.md ./
COPY --chown=1001:0 src ./src
RUN pip install --no-cache-dir .

WORKDIR /data
ENTRYPOINT ["rvtools-hw-check"]
CMD ["--help"]
