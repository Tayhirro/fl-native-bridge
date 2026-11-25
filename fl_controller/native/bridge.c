#include "native.h"
#include <string.h>

#ifndef FLMCP_MODULE_NAME
#define FLMCP_MODULE_NAME "_flmcp_native"
#define FLMCP_MODULE_INIT PyInit__flmcp_native
#endif

#ifndef Py_mod_multiple_interpreters
#  define Py_mod_multiple_interpreters 3
#  define Py_MOD_PER_INTERPRETER_GIL_SUPPORTED ((void *)2)
#endif

static PyObject *describe(PyObject *self, PyObject *unused) {
    (void)unused;
    ModuleState *state = PyModule_GetState(self);
    PyObject *dependencies = fl_describe_dependencies();
    if (!dependencies) {
        return NULL;
    }
    PyObject *result = Py_BuildValue("{s:i,s:s,s:[sssssssssssssssssssssssssssssssssss],s:O}",
                         "api_version", 1,
                         "engine_profile", "26.1.7.5654-x64",
                         "operations", "mixer.insert_empty_slot", "playlist.read_clips",
                         "playlist.move_clip", "playlist.insert_clip",
                         "playlist.delete_clip", "playlist.duplicate_clip",
                         "playlist.set_clip_muted", "playlist.trim_clip", "automation.read_points",
                         "automation.set_point_value", "automation.set_point_tension",
                         "automation.move_point", "automation.edit_points",
                         "automation.set_point_curve",
                         "automation.vtable_dump",
                         "automation.read_links", "automation.create_source",
                         "channel.add_sampler", "channel.add_generator",
                         "channel.replace_sample",
                         "pattern.read_notes", "pattern.add_notes", "pattern.write_notes",
                         "timeline.read_markers", "timeline.add_marker",
                         "timeline.delete_marker", "timeline.refresh",
                         "arrangement.read_all", "arrangement.add",
                         "arrangement.clone", "arrangement.delete", "arrangement.set_current",
                         "arrangement.set_name", "project.save_copy",
                         "mixer.remove_effect",
                         "dependencies", dependencies);
    Py_DECREF(dependencies);
    (void)state;
    return result;
}

static PyObject *execute(PyObject *self, PyObject *args) {
    const char *operation;
    PyObject *arguments;
    if (!PyArg_ParseTuple(args, "sO:execute", &operation, &arguments)) {
        return NULL;
    }
    /* Dependency gate runs before any handler so a refused operation never
     * touches FL memory, even if its handler has a partial write path. */
    ModuleState *state = PyModule_GetState(self);
    const char *gate_error = fl_dependency_gate(operation, state->engine);
    if (gate_error) {
        PyErr_Format(PyExc_RuntimeError, "Operation %s refused: %s", operation, gate_error);
        return NULL;
    }
    if (!strcmp(operation, "mixer.insert_empty_slot")) {
        return mixer_insert_empty_slot(self, arguments);
    }
    if (!strcmp(operation, "channel.add_sampler")) {
        return channel_add_sampler(self, arguments);
    }
    if (!strcmp(operation, "channel.add_generator")) {
        return channel_add_generator(self, arguments);
    }
    if (!strcmp(operation, "channel.replace_sample")) {
        return channel_replace_sample(self, arguments);
    }
    if (!strcmp(operation, "playlist.read_clips")) {
        return playlist_read_clips(self, arguments);
    }
    if (!strcmp(operation, "playlist.move_clip")) {
        return playlist_move_clip(self, arguments);
    }
    if (!strcmp(operation, "playlist.insert_clip")) {
        return playlist_insert_clip(self, arguments);
    }
    if (!strcmp(operation, "playlist.delete_clip")) {
        return playlist_delete_clip(self, arguments);
    }
    if (!strcmp(operation, "playlist.duplicate_clip")) {
        return playlist_duplicate_clip(self, arguments);
    }
    if (!strcmp(operation, "playlist.set_clip_muted")) {
        return playlist_set_clip_muted(self, arguments);
    }
    if (!strcmp(operation, "playlist.trim_clip")) {
        return playlist_trim_clip(self, arguments);
    }
    if (!strcmp(operation, "automation.read_points")) {
        return automation_read_points(self, arguments);
    }
    if (!strcmp(operation, "automation.set_point_value")) {
        return automation_set_point_value(self, arguments);
    }
    if (!strcmp(operation, "automation.set_point_tension")) {
        return automation_set_point_tension(self, arguments);
    }
    if (!strcmp(operation, "automation.move_point")) {
        return automation_move_point(self, arguments);
    }
    if (!strcmp(operation, "automation.edit_points")) {
        return automation_edit_points(self, arguments);
    }
    if (!strcmp(operation, "automation.set_point_curve")) {
        return automation_set_point_curve(self, arguments);
    }
    if (!strcmp(operation, "automation.vtable_dump")) {
        return automation_vtable_dump(self, arguments);
    }
    if (!strcmp(operation, "automation.read_links")) {
        return automation_read_links(self, arguments);
    }
    if (!strcmp(operation, "automation.create_source")) {
        return automation_create_source(self, arguments);
    }
    if (!strcmp(operation, "pattern.read_notes")) {
        return pattern_read_notes(self, arguments);
    }
    if (!strcmp(operation, "pattern.add_notes")) {
        return pattern_add_notes(self, arguments);
    }
    if (!strcmp(operation, "pattern.write_notes")) {
        return pattern_write_notes(self, arguments);
    }
    if (!strcmp(operation, "timeline.read_markers")) {
        return timeline_read_markers(self, arguments);
    }
    if (!strcmp(operation, "timeline.add_marker")) {
        return timeline_add_marker(self, arguments);
    }
    if (!strcmp(operation, "timeline.delete_marker")) {
        return timeline_delete_marker(self, arguments);
    }
    if (!strcmp(operation, "timeline.refresh")) {
        return timeline_refresh(self, NULL);
    }
    if (!strcmp(operation, "arrangement.read_all")) {
        return arrangement_read_all(self, arguments);
    }
    if (!strcmp(operation, "arrangement.add")) {
        return arrangement_add(self, arguments);
    }
    if (!strcmp(operation, "arrangement.clone")) {
        return arrangement_clone(self, arguments);
    }
    if (!strcmp(operation, "arrangement.set_current")) {
        return arrangement_set_current(self, arguments);
    }
    if (!strcmp(operation, "arrangement.delete")) {
        return arrangement_delete(self, arguments);
    }
    if (!strcmp(operation, "arrangement.set_name")) {
        return arrangement_set_name(self, arguments);
    }
    if (!strcmp(operation, "project.save_copy")) {
        return project_save_copy(self, arguments);
    }
    if (!strcmp(operation, "mixer.remove_effect")) {
        return mixer_remove_effect(self, arguments);
    }
    PyErr_SetString(PyExc_ValueError, "Unknown native operation");
    return NULL;
}

static PyObject *test_override(PyObject *self, PyObject *args) {
    (void)self;
    const char *operation, *field, *state = NULL;
    if (!PyArg_ParseTuple(args, "ss|z:test_override", &operation, &field, &state)) {
        return NULL;
    }
    return PyLong_FromLong(fl_test_override(operation, field, state));
}

static PyMethodDef methods[] = {
    {"initialize", initialize_engine, METH_NOARGS, "Validate the running FL engine."},
    {"describe", describe, METH_NOARGS, "Describe compiled native operations."},
    {"execute", execute, METH_VARARGS, "Execute a named native operation."},
    {"test_override", test_override, METH_VARARGS,
     "Test-only: downgrade one evidence field; cannot raise the gate."},
    {NULL, NULL, 0, NULL},
};

static PyModuleDef_Slot slots[] = {
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
    {0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, FLMCP_MODULE_NAME, "FL Studio native operation bridge.",
    sizeof(ModuleState), methods, slots, NULL, NULL, NULL,
};

PyMODINIT_FUNC FLMCP_MODULE_INIT(void) {
    return PyModuleDef_Init(&module);
}
