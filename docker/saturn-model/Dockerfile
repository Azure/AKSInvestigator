# Build context: the directory containing model/, extracted from the same
# ModelPack OCI layout published by publish-saturn-model.yaml.
FROM mcr.microsoft.com/azurelinux/base/core:3.0

ARG HF_MODEL=saturn-ms/saturn-0202
ARG MODEL_REVISION
LABEL org.opencontainers.image.title="Saturn model data" \
      org.opencontainers.image.source="https://huggingface.co/${HF_MODEL}" \
      org.opencontainers.image.revision="${MODEL_REVISION}"

# Keep model files outside /models, which is hidden by the pod's emptyDir mount.
# No vLLM, ORAS, Python, credentials or download step is needed at runtime.
COPY model/ /model/
ENTRYPOINT ["/bin/sh", "-ec", "test \"$#\" -eq 1; test -n \"$1\"; mkdir -p -- \"$1\"; cp -R /model/. \"$1\"/", "copy-model"]
CMD ["/models/saturn-0202"]
