#!/usr/bin/env python3
"""Apply Quest KGSL timeline and opaque GL memory-import fixes to Mesa 26.1.6."""
import argparse
from pathlib import Path
import shutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("mesa", type=Path)
args = parser.parse_args()
repo = Path(__file__).resolve().parents[2]
target = args.mesa / "src/freedreno/vulkan/tu_knl_kgsl.cc"
source = target.read_text()
if '#include "mesa-shared-timeline.h"' not in source:
    replacements = {
        "static const struct tu_knl kgsl_knl_funcs = {":
            '#include "mesa-shared-timeline.h"\n\nstatic const struct tu_knl kgsl_knl_funcs = {',
        ".queue_submit = kgsl_queue_submit,": ".queue_submit = quest_kgsl_queue_submit,",
        "device->sync_types[1] = &device->timeline_type.sync;":
            "device->sync_types[1] = &quest_timeline_type;",
        "if (wait_flags & VK_SYNC_WAIT_PENDING)\n      return VK_SUCCESS;":
            "/* Conservative pending waits also wait for GPU completion. */",
    }
    for before, after in replacements.items():
        expected = 2 if before.startswith("if (wait_flags") else 1
        if source.count(before) != expected:
            raise SystemExit(f"Unsupported Mesa source: expected {expected} occurrences of {before!r}")
        source = source.replace(before, after)
    target.write_text(source)
shutil.copyfile(repo / "packages/quest-runtime/linux/mesa-shared-timeline.h",
                target.parent / "mesa-shared-timeline.h")

# Allocation helpers own the DMA-heap/ION fd. The importer duplicates it for
# the BO, so retaining the original pins GPU memory after vkFreeMemory.
source = target.read_text()
for function, fd in (("bo_init_new_dmaheap", "alloc.fd"), ("bo_init_new_ion", "alloc.fd"),
                     ("bo_init_new_ion_legacy", "share.fd")):
    start = source.index(function + "(")
    end = source.index("\nstatic ", start)
    block = source[start:end]
    before = f"return tu_bo_init_dmabuf(dev, out_bo, -1, TU_BO_ALLOC_NO_FLAGS, {fd});"
    after = (f"VkResult result = tu_bo_init_dmabuf(dev, out_bo, -1, TU_BO_ALLOC_NO_FLAGS, {fd});\n"
             f"   close({fd});\n   return result;")
    if before in block:
        if block.count(before) != 1:
            raise SystemExit(f"Unsupported Mesa allocation helper: {function}")
        source = source[:start] + block.replace(before, after) + source[end:]
    elif f"close({fd});" not in block or "return result;" not in block:
        raise SystemExit(f"Unsupported Mesa allocation ownership: {function}")
target.write_text(source)

# Adreno 740v3 (Quest 3 / Quest 3S Horizon KGSL): IOCTL_KGSL_GPUMEM_ALLOC_ID
# and IOCTL_KGSL_GPUMEM_GET_INFO are denied for untrusted apps (EACCES), so
# vkCreateDevice fails at the global BO with VK_ERROR_OUT_OF_DEVICE_MEMORY.
# IOCTL_KGSL_GPUOBJ_ALLOC, IOCTL_KGSL_GPUOBJ_INFO and id<<12 mmap of the full
# object succeed and return a kernel-assigned GPU address. This section reroutes
# the KGSL BO allocation through GPUOBJ when GPUMEM is denied, tags those BOs
# for GPUOBJ_FREE, and logs every allocation attempt with a QUESTKGSL tag.
source = target.read_text()
if "quest_kgsl_log" not in source:
    helpers = '''#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

static void
quest_kgsl_log(const char *step, uint64_t size, uint64_t flags, int err)
{
   if (err) {
      fprintf(stderr, "QUESTKGSL %s size=%" PRIu64 " flags=0x%" PRIx64
              " errno=%d (%s)\\n", step, size, flags, err, strerror(err));
   } else {
      fprintf(stderr, "QUESTKGSL %s size=%" PRIu64 " flags=0x%" PRIx64 " ok\\n",
              step, size, flags);
   }
}

static int
quest_kgsl_gpuobj_alloc(struct tu_device *dev, uint64_t size,
                        VkMemoryPropertyFlags mem_property, uint64_t flags,
                        struct kgsl_gpumem_alloc_id *req)
{
   struct kgsl_gpuobj_alloc qreq = {
      .size = size,
      .flags = flags,
      .va_len = 0,
   };
   errno = 0;
   int ret = safe_ioctl(dev->physical_device->local_fd,
                        IOCTL_KGSL_GPUOBJ_ALLOC, &qreq);
   quest_kgsl_log(ret ? "GPUOBJ_ALLOC failed" : "GPUOBJ_ALLOC ok",
                  size, qreq.flags, ret ? errno : 0);
   if (ret)
      return ret;
   struct kgsl_gpuobj_info info = {
      .id = qreq.id,
   };
   errno = 0;
   ret = safe_ioctl(dev->physical_device->local_fd,
                    IOCTL_KGSL_GPUOBJ_INFO, &info);
   if (ret) {
      quest_kgsl_log("GPUOBJ_INFO failed", size, qreq.flags, errno);
      struct kgsl_gpuobj_free free_req = {
         .id = qreq.id,
      };
      safe_ioctl(dev->physical_device->local_fd, IOCTL_KGSL_GPUOBJ_FREE, &free_req);
      return ret;
   }
   req->id = qreq.id;
   req->mmapsize = qreq.mmapsize;
   req->gpuaddr = info.gpuaddr;
   req->flags = 0;
   return 0;
}
'''
    start = source.index("\nstatic VkResult\nkgsl_bo_init(struct tu_device *dev,")
    source = source[:start] + "\n" + helpers + source[start:]

    before = '''   ret = safe_ioctl(dev->physical_device->local_fd,
                    IOCTL_KGSL_GPUMEM_ALLOC_ID, &req);
   if (ret) {
      return vk_errorf(dev, VK_ERROR_OUT_OF_DEVICE_MEMORY,
                       "GPUMEM_ALLOC_ID failed (%s)", strerror(errno));
   }
'''
    after = '''   bool used_gpuobj = false;
   ret = safe_ioctl(dev->physical_device->local_fd,
                    IOCTL_KGSL_GPUMEM_ALLOC_ID, &req);
   if (ret) {
      quest_kgsl_log("GPUMEM_ALLOC_ID failed", size, req.flags, errno);
      uint64_t quest_flags = 0;
      if (mem_property & VK_MEMORY_PROPERTY_HOST_CACHED_BIT) {
         if (mem_property & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)
            quest_flags |= KGSL_MEMFLAGS_IOCOHERENT;
         quest_flags |= (uint64_t)KGSL_CACHEMODE_WRITEBACK << KGSL_CACHEMODE_SHIFT;
      } else {
         quest_flags |= (uint64_t)KGSL_CACHEMODE_WRITECOMBINE << KGSL_CACHEMODE_SHIFT;
      }
      ret = quest_kgsl_gpuobj_alloc(dev, size, mem_property, quest_flags, &req);
      if (ret) {
         quest_kgsl_log("retrying GPUOBJ without flags", size, 0, 0);
         ret = quest_kgsl_gpuobj_alloc(dev, size, 0, 0, &req);
      }
      if (ret) {
         return vk_errorf(dev, VK_ERROR_OUT_OF_DEVICE_MEMORY,
                          "GPUMEM_ALLOC_ID failed (%s)", strerror(errno));
      }
      used_gpuobj = true;
   }
'''
    if source.count(before) != 1:
        raise SystemExit("Unsupported Mesa source: GPUMEM_ALLOC_ID block not found")
    source = source.replace(before, after)

    init_before = '''   struct tu_bo* bo = tu_device_lookup_bo(dev, req.id);
   assert(bo && bo->gem_handle == 0);

   *bo = (struct tu_bo) {
      .gem_handle = req.id,
      .size = req.mmapsize,
      .iova = req.gpuaddr,
      .name = tu_debug_bos_add(dev, req.mmapsize, name),
      .refcnt = 1,
      .shared_fd = -1,
      .base = base,
   };
'''
    init_after = '''   struct tu_bo* bo = tu_device_lookup_bo(dev, req.id);
   assert(bo && bo->gem_handle == 0);

   *bo = (struct tu_bo) {
      .gem_handle = req.id,
      .size = req.mmapsize,
      .iova = req.gpuaddr,
      .name = tu_debug_bos_add(dev, req.mmapsize, name),
      .refcnt = 1,
      .shared_fd = -1,
      .quest_gpuobj = used_gpuobj,
      .base = base,
   };
'''
    if source.count(init_before) != 1:
        raise SystemExit("Unsupported Mesa source: kgsl_bo_init bo construction not found")
    source = source.replace(init_before, init_after)

    finish_before = '''   struct kgsl_gpumem_free_id req = {
      .id = bo->gem_handle
   };

   /* Tell sparse array that entry is free */
   memset(bo, 0, sizeof(*bo));

   safe_ioctl(dev->physical_device->local_fd, IOCTL_KGSL_GPUMEM_FREE_ID, &req);
}

static VkResult
kgsl_sparse_vma_init(struct tu_device *dev,'''
    finish_after = '''   if (bo->quest_gpuobj) {
      struct kgsl_gpuobj_free req = {
         .id = bo->gem_handle
      };
      memset(bo, 0, sizeof(*bo));
      safe_ioctl(dev->physical_device->local_fd, IOCTL_KGSL_GPUOBJ_FREE, &req);
   } else {
      struct kgsl_gpumem_free_id req = {
         .id = bo->gem_handle
      };

      /* Tell sparse array that entry is free */
      memset(bo, 0, sizeof(*bo));

      safe_ioctl(dev->physical_device->local_fd, IOCTL_KGSL_GPUMEM_FREE_ID, &req);
   }
}

static VkResult
kgsl_sparse_vma_init(struct tu_device *dev,'''
    if source.count(finish_before) != 1:
        raise SystemExit("Unsupported Mesa source: kgsl_bo_finish not found")
    source = source.replace(finish_before, finish_after)

    map_before = '''   if (map == MAP_FAILED)
      return vk_error(dev, VK_ERROR_MEMORY_MAP_FAILED);'''
    map_after = '''   if (map == MAP_FAILED) {
      quest_kgsl_log("mmap failed", bo->size, bo->gem_handle, errno);
      return vk_error(dev, VK_ERROR_MEMORY_MAP_FAILED);
   }'''
    if source.count(map_before) != 1:
        raise SystemExit("Unsupported Mesa source: bo map block not found")
    source = source.replace(map_before, map_after)

    # Capability probes (HOST_CACHED coherent memory, USE_CPU_MAP) allocated a
    # throws-away page through IOCTL_KGSL_GPUMEM_ALLOC_ID. Horizon KGSL denies
    # GPUMEM to untrusted apps (EACCES), so Turnip concluded cached memory was
    # unsupported and SteamVR's compositor failed with "Failed to determine a
    # compatible memory type". Probe through GPUOBJ_ALLOC instead: it works
    # under the same app sandbox and echoes the accepted flags like GPUMEM.
    probe_before = '''   int ret = safe_ioctl(fd, IOCTL_KGSL_GPUMEM_ALLOC_ID, &req_alloc);
   if (ret) {
      return false;
   }

   /* The kernel echoes back the *actual* flags it used. Some KGSL
    * versions silently strip unsupported flags (e.g. IOCOHERENT on
    * GPUs that lack IO-coherence) instead of failing the ioctl.
    * Detect this by checking the requested bits are still present.
    */
   bool supported = (req_alloc.flags & flags) == flags;

   struct kgsl_gpumem_free_id req_free = { .id = req_alloc.id };

   safe_ioctl(fd, IOCTL_KGSL_GPUMEM_FREE_ID, &req_free);

   return supported;
'''
    probe_after = '''   int ret = safe_ioctl(fd, IOCTL_KGSL_GPUMEM_ALLOC_ID, &req_alloc);
   struct kgsl_gpuobj_alloc req_probe = {
      .size = req_alloc.size,
      .flags = req_alloc.flags,
   };
   bool probe_gpuobj = false;
   if (ret) {
      ret = safe_ioctl(fd, IOCTL_KGSL_GPUOBJ_ALLOC, &req_probe);
      if (ret) {
         return false;
      }
      probe_gpuobj = true;
   }

   /* The kernel echoes back the *actual* flags it used. Some KGSL
    * versions silently strip unsupported flags (e.g. IOCOHERENT on
    * GPUs that lack IO-coherence) instead of failing the ioctl.
    * Detect this by checking the requested bits are still present.
    */
   if (probe_gpuobj)
      req_alloc.flags = req_probe.flags;

   bool supported = (req_alloc.flags & flags) == flags;

   if (probe_gpuobj) {
      struct kgsl_gpuobj_free req_free = { .id = req_probe.id };
      safe_ioctl(fd, IOCTL_KGSL_GPUOBJ_FREE, &req_free);
   } else {
      struct kgsl_gpumem_free_id req_free = { .id = req_alloc.id };
      safe_ioctl(fd, IOCTL_KGSL_GPUMEM_FREE_ID, &req_free);
   }

   return supported;
'''
    if source.count(probe_before) != 1:
        raise SystemExit("Unsupported Mesa source: kgsl_is_memory_type_supported not found")
    source = source.replace(probe_before, probe_after)

    target.write_text(source)

# Tag GPUOBJ-allocated BOs so kgsl_bo_finish frees them with the right ioctl.
knl_header = args.mesa / "src/freedreno/vulkan/tu_knl.h"
header_source = knl_header.read_text()
if "quest_gpuobj" not in header_source:
    before = "   bool lazy : 1;\n"
    after = "   bool quest_gpuobj : 1;\n   bool lazy : 1;\n"
    if header_source.count(before) != 1:
        raise SystemExit("Unsupported Mesa source: tu_bo bitfield not found")
    knl_header.write_text(header_source.replace(before, after))

# An opaque GL memory fd carries no explicit DRM modifier. The state tracker
# initializes winsys_handle to zero, which otherwise looks like linear tiling
# and makes Zink reinterpret an optimal Vulkan image as a linear dma-buf.
zink = args.mesa / "src/gallium/drivers/zink/zink_resource.c"
source = zink.read_text()
before = "memobj->whandle.type = ZINK_EXTERNAL_MEMORY_HANDLE;"
after = before + "\n   memobj->whandle.modifier = DRM_FORMAT_MOD_INVALID;"
if after not in source:
    if source.count(before) != 1:
        raise SystemExit("Unsupported Mesa Zink memory-object import")
    zink.write_text(source.replace(before, after))
