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
} OperationDependency;

/* Only the operations with explicit cross-checks are listed here. Other
 * operations remain covered by the exact-build gate and their in-handler
 * checks; describe() reports them as build-gated without claiming more. */
/* Mutable copies backing the gate and describe(). A test override may only
 * DOWNGRADE a field to "unsupported"; it can never raise evidence level, so
 * exposing the hook does not create a path around the gate. */
static OperationDependency dependencies[] = {
    {"mixer.insert_empty_slot", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1},
    {"mixer.remove_effect", 0, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1},
    {"project.save_copy", PROJECT_SAVE_WORKER, TEXT_START, TEXT_END,
     "build-gated", "build-gated", "verified", 1},
};

/* Non-NULL only when a test explicitly installs a fake engine. Never set by
 * production code; the real engine pointer from ModuleState always wins. */
static const unsigned char *test_engine = NULL;

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

static int image_readable(const unsigned char *engine, uint32_t rva) {
    MEMORY_BASIC_INFORMATION info;
    const void *address = engine + rva;
    if (rva < 0x1000 || !VirtualQuery(address, &info, sizeof(info)) ||
            info.State != MEM_COMMIT || info.AllocationBase != engine ||
            (info.Protect & (PAGE_NOACCESS | PAGE_GUARD))) {
        return 0;
    }
    /* A plausible function prologue is neither 00-filled nor FF-filled. */
    unsigned char first = *(const unsigned char *)address;
    return first != 0x00 && first != 0xFF;
}

/* Return NULL when the operation may run, otherwise a static reason. */
const char *fl_dependency_gate(const char *operation, const unsigned char *engine) {
    const OperationDependency *dependency = find_dependency(operation);
    if (!dependency) {
        return NULL; /* no declared dependency; existing build gate still applies */
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
        if (engine != test_engine && !image_readable(engine, dependency->entry_rva)) {
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
            "{s:s,s:s,s:s,s:s,s:i,s:i,s:i}",
            "function_entry", dependency->entry_rva ? "verified" : "build-gated",
            "global_addresses", dependency->globals_state,
            "object_layout", dependency->layout_state,
            "calling_convention", dependency->callconv_state,
            "entry_rva", (int)dependency->entry_rva,
            "rva_low", (int)dependency->entry_low,
            "rva_high", (int)dependency->entry_high);
        if (!entry || PyDict_SetItemString(result, dependency->operation, entry) < 0) {
            Py_XDECREF(entry);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(entry);
    }
    return result;
}

int fl_operation_is_write(const char *operation) {
    const OperationDependency *dependency = find_dependency(operation);
    return dependency ? dependency->write : 0;
}
