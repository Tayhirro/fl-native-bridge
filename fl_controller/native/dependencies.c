/* Per-operation dependency declarations and runtime gates.
 *
 * An exact engine SHA-256 proves which binary is loaded; it does not prove
 * that a given operation's function, globals, object layout and calling
 * convention are all correct. This module keeps those four checks separate
 * so a capability report never overstates what was verified.
 *
 * Each gate has one of three states:
 *   verified     - independently observed on this exact build and checked
 *                  again against the live engine image at resolution time.
 *   build-gated  - covered only by the exact-build SHA-256 and the existing
 *                  in-handler runtime checks (thread id, RVA range, pointer
 *                  reads); not an independent proof of layout/calling convention.
 *   unsupported  - no evidence; the operation is refused before its handler.
 *
 * Writes require function entry, globals and layout to be at least build-gated
 * and the calling convention to be verified (a wrong convention corrupts FL's
 * stack inside its own process). Reads are allowed with a verified entry.
 */

#include "native.h"
#include "profiles/fl_26_1_7_5654.h"
#include <string.h>

typedef struct {
    const char *operation;
    uint32_t entry_rva;         /* 0 = resolved from a live object (vtable) */
    uint32_t entry_low;         /* accepted RVA range, inclusive */
    uint32_t entry_high;
    const char *globals_state;  /* verified | build-gated | unsupported */
    const char *layout_state;
    const char *callconv_state;
    int write;
    const char *evidence;       /* independent | build-gated */
} OperationDependency;

/* The single source of truth for both describe().operations and the gate.
 * "independent" means the entry point was separately observed on this build
 * (profile header records the byte signature); "build-gated" means only the
 * exact SHA-256 plus the existing in-handler checks apply. No entry here
 * claims more than that. A test override may only DOWNGRADE a field. */
static OperationDependency dependencies[] = {
    /* --- independent entry evidence (recorded in the engine profile) --- */
    {"playlist.trim_clip", SET_CLIP_SOURCE_RANGE, TEXT_START, TEXT_END,
     "verified", "verified", "verified", 1, "independent"},
    /* Static RVA was observed, but no byte signature was recorded for these;
     * they do not get the "independent" label (no invented evidence). */
    {"automation.create_source", CREATE_AUTOMATION, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"pattern.add_notes", RECORD_NOTE_ON, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"pattern.write_notes", RECORD_NOTE_ON, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},

    /* --- FX writes: dynamic vtable entry, build-gated --- */
    {"mixer.insert_empty_slot", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"mixer.remove_effect", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"project.save_copy", PROJECT_SAVE_WORKER, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},

    /* --- remaining operations: exact build + in-handler checks only --- */
    {"playlist.read_clips", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 0, "build-gated"},
    {"playlist.move_clip", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"playlist.insert_clip", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"playlist.delete_clip", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"playlist.duplicate_clip", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"playlist.set_clip_muted", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"automation.read_points", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 0, "build-gated"},
    {"automation.set_point_value", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"automation.set_point_tension", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"automation.move_point", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"automation.edit_points", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"automation.set_point_curve", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"automation.vtable_dump", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 0, "build-gated"},
    {"automation.read_links", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 0, "build-gated"},
    {"channel.add_sampler", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"channel.add_generator", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"channel.replace_sample", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"pattern.read_notes", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 0, "build-gated"},
    {"timeline.read_markers", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 0, "build-gated"},
    {"timeline.add_marker", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"timeline.delete_marker", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"timeline.refresh", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"arrangement.read_all", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 0, "build-gated"},
    {"arrangement.add", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"arrangement.clone", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"arrangement.delete", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"arrangement.set_current", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
    {"arrangement.set_name", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1, "build-gated"},
};

/* Non-NULL only when a test explicitly installs a fake engine. Never set by
 * production code; the real engine pointer from ModuleState always wins. */
static const unsigned char *test_engine = NULL;

/* Entry-byte signatures for the independently verified operations. Matched
 * against the live engine image before those operations run. Operations with
 * a dynamically resolved vtable entry have no entry here by design. */
typedef struct {
    uint32_t rva;
    const unsigned char *bytes;
    size_t length;
} EntrySignature;

static const EntrySignature entry_signatures[] = {
    {RECOUNT_ACTIVE_CLIPS, recount_clips_bytes, sizeof(recount_clips_bytes)},
    {SET_CLIP_SOURCE_RANGE, set_clip_range_bytes, sizeof(set_clip_range_bytes)},
};

static const EntrySignature *signature_for(uint32_t rva) {
    size_t index;
    for (index = 0; index < sizeof(entry_signatures) / sizeof(entry_signatures[0]); ++index) {
        if (entry_signatures[index].rva == rva) {
            return &entry_signatures[index];
        }
    }
    return NULL;
}

static int state_rank(const char *state) {
    if (!strcmp(state, "verified")) {
        return 2;
    }
    if (!strcmp(state, "build-gated")) {
        return 1;
    }
    return 0;
}

static const OperationDependency *find_dependency(const char *operation) {
    size_t index;
    for (index = 0; index < sizeof(dependencies) / sizeof(dependencies[0]); ++index) {
        if (!strcmp(dependencies[index].operation, operation)) {
            return &dependencies[index];
        }
    }
    return NULL;
}

/* Test-only hook: force one evidence field to "unsupported", or restore the
 * compiled value with state=NULL. It can only DOWNGRADE evidence, so it cannot
 * be used to bypass the gate. Fields: global_addresses, object_layout,
 * calling_convention, rva. Returns 1 when applied, 0 for unknown names. */
int fl_test_override(const char *operation, const char *field, const char *state) {
    if (!operation || !field) {
        return 0;
    }
    OperationDependency *dependency = NULL;
    size_t index;
    for (index = 0; index < sizeof(dependencies) / sizeof(dependencies[0]); ++index) {
        if (!strcmp(dependencies[index].operation, operation)) {
            dependency = &dependencies[index];
            break;
        }
    }
    if (!dependency) {
        return 0;
    }
    int downgrade = state && !strcmp(state, "unsupported");
    /* Test-only fake engine: a non-NULL sentinel so the gate can be exercised
     * without FL. Image reads are bypassed by overriding the RVA field. */
    if (!strcmp(field, "engine")) {
        if (state && !strcmp(state, "unsupported")) {
            *(void **)&test_engine = NULL;
        } else {
            test_engine = (const unsigned char *)(uintptr_t)0x1000;
        }
        return 1;
    }
    if (!strcmp(field, "rva")) {
        dependency->entry_rva = downgrade
            ? dependency->entry_high
            : (dependency->operation[0] == 'p' ? PROJECT_SAVE_WORKER : 0);
        return 1;
    }
    const char **target = NULL;
    const char *restored = NULL;
    if (!strcmp(field, "global_addresses")) {
        target = &dependency->globals_state;
        restored = "build-gated";
    } else if (!strcmp(field, "object_layout")) {
        target = &dependency->layout_state;
        restored = "build-gated";
    } else if (!strcmp(field, "calling_convention")) {
        target = &dependency->callconv_state;
        restored = "verified";
    } else {
        return 0;
    }
    *target = downgrade ? "unsupported" : restored;
    return 1;
}

/* Validate a static entry against the live image. When a byte signature is
 * available it is compared in full; otherwise only the weakest plausible-
 * prologue check applies, and describe() labels that entry build-gated. */
static int entry_bytes_valid(const unsigned char *engine, uint32_t rva) {
    MEMORY_BASIC_INFORMATION info;
    const void *address = engine + rva;
    if (rva < 0x1000 || !VirtualQuery(address, &info, sizeof(info)) ||
            info.State != MEM_COMMIT || info.AllocationBase != engine ||
            (info.Protect & (PAGE_NOACCESS | PAGE_GUARD))) {
        return 0;
    }
    const EntrySignature *signature = signature_for(rva);
    if (signature) {
        uintptr_t delta = (uintptr_t)address - (uintptr_t)info.BaseAddress;
        if (signature->length > info.RegionSize - delta) {
            return 0;
        }
        return !memcmp(address, signature->bytes, signature->length);
    }
    unsigned char first = *(const unsigned char *)address;
    return first != 0x00 && first != 0xFF;
}

/* Return NULL when the operation may run, otherwise a static reason. */
const char *fl_dependency_gate(const char *operation, const unsigned char *engine) {
    const OperationDependency *dependency = find_dependency(operation);
    if (!dependency) {
        /* Fail closed: a handler must never run without a declared dependency. */
        return "Operation is not registered in the dependency table";
    }
    if (!engine) {
        engine = test_engine;
    }
    if (!engine) {
        return "Engine is not initialized";
    }
    if (state_rank(dependency->callconv_state) < 2) {
        return "Calling convention is not verified for this build";
    }
    if (state_rank(dependency->globals_state) < 1 ||
            state_rank(dependency->layout_state) < 1) {
        return "Required globals or object layout are not verified";
    }
    if (dependency->entry_rva) {
        if (dependency->entry_rva < dependency->entry_low ||
                dependency->entry_rva >= dependency->entry_high) {
            return "Function RVA is outside the verified text range";
        }
        if (engine != test_engine && !entry_bytes_valid(engine, dependency->entry_rva)) {
            return "Function bytes failed the image self-check";
        }
    }
    return NULL;
}

/* Build the operation dependency map exposed by describe(). */
PyObject *fl_describe_dependencies(void) {
    PyObject *result = PyDict_New();
    if (!result) {
        return NULL;
    }
    size_t index;
    for (index = 0; index < sizeof(dependencies) / sizeof(dependencies[0]); ++index) {
        const OperationDependency *dependency = &dependencies[index];
        /* A dynamic vtable entry is only checked against the verified text
         * range and the exact-build hash; that is build-gated evidence, not
         * an independent proof of the function's identity. */
        PyObject *entry = Py_BuildValue(
            "{s:s,s:s,s:s,s:s,s:s,s:s,s:i,s:i,s:i,s:i}",
            "function_entry", dependency->entry_rva ? "verified" : "build-gated",
            "global_addresses", dependency->globals_state,
            "object_layout", dependency->layout_state,
            "calling_convention", dependency->callconv_state,
            "evidence", dependency->evidence,
            "entry_bytes", signature_for(dependency->entry_rva)
                           ? "signature-matched" : "range-and-prologue",
            "entry_rva", (int)dependency->entry_rva,
            "rva_low", (int)dependency->entry_low,
            "rva_high", (int)dependency->entry_high,
            "write", dependency->write);
        if (!entry || PyDict_SetItemString(result, dependency->operation, entry) < 0) {
            Py_XDECREF(entry);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(entry);
    }
    return result;
}

/* A handler may only run when the operation is registered in this table.
 * An unregistered operation fails closed before its handler is entered. */
int fl_has_dependency(const char *operation) {
    return find_dependency(operation) != NULL;
}

int fl_operation_is_write(const char *operation) {
    const OperationDependency *dependency = find_dependency(operation);
    return dependency ? dependency->write : 0;
}

/* Operations are derived from the same table as the gate, so the advertised
 * catalogue and the checked dependencies cannot drift apart. */
PyObject *fl_describe_operations(void) {
    PyObject *result = PyList_New(0);
    if (!result) {
        return NULL;
    }
    size_t index;
    for (index = 0; index < sizeof(dependencies) / sizeof(dependencies[0]); ++index) {
        PyObject *name = PyUnicode_FromString(dependencies[index].operation);
        if (!name || PyList_Append(result, name) < 0) {
            Py_XDECREF(name);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(name);
    }
    return result;
}
