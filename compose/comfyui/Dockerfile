# ComfyUI runtime + the C toolchain SageAttention needs.
#
#   docker build -t comfyui-local:tier2-gcc compose/comfyui
#
# Why this layer exists. SageAttention — `attention_sage` in ComfyUI-KJNodes, which the Wan 2.2
# video workflows select — JIT-compiles its Triton kernels on first use, and Triton shells out to a
# C compiler to build its CUDA driver shim. comfyui-local:tier2 ships no compiler, so `gcc` had been
# apt-installed by hand INSIDE the running container. That state lived only in the container's
# writable layer, which meant any recreate, image rebuild or `compose up --force-recreate` would
# silently break video generation while leaving images working — the failure surfaces as
# `RuntimeError in KSamplerAdvanced — Failed to find C compiler`, several nodes away from the cause.
#
# `docker diff` on the old container showed exactly 27 added packages: gcc, g++ and their
# dependencies, nothing else. Installing the two pulls the other 25.
#
# See docs/TROUBLESHOOTING.md, "Every image and video fails, but the GPU looks idle".
FROM comfyui-local:tier2

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc g++ \
 && rm -rf /var/lib/apt/lists/*
