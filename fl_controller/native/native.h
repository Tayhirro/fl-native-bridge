#ifndef FLMCP_NATIVE_H
#define FLMCP_NATIVE_H

#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030A0000
#include <Python.h>
#include <windows.h>
#include <stdint.h>

typedef struct {
    unsigned char *engine;
} ModuleState;

PyObject *initialize_engine(PyObject *self, PyObject *unused);
PyObject *mixer_insert_empty_slot(PyObject *self, PyObject *args);
PyObject *channel_add_sampler(PyObject *self, PyObject *args);
PyObject *channel_add_generator(PyObject *self, PyObject *args);
PyObject *channel_replace_sample(PyObject *self, PyObject *args);
PyObject *playlist_read_clips(PyObject *self, PyObject *args);
PyObject *playlist_move_clip(PyObject *self, PyObject *args);
PyObject *playlist_insert_clip(PyObject *self, PyObject *args);
PyObject *playlist_delete_clip(PyObject *self, PyObject *args);
PyObject *playlist_duplicate_clip(PyObject *self, PyObject *args);
PyObject *playlist_set_clip_muted(PyObject *self, PyObject *args);
PyObject *playlist_trim_clip(PyObject *self, PyObject *args);
PyObject *automation_read_points(PyObject *self, PyObject *args);
PyObject *automation_set_point_value(PyObject *self, PyObject *args);
PyObject *automation_set_point_tension(PyObject *self, PyObject *args);
PyObject *automation_move_point(PyObject *self, PyObject *args);
PyObject *automation_set_point_curve(PyObject *self, PyObject *args);
PyObject *automation_edit_points(PyObject *self, PyObject *args);
PyObject *automation_read_links(PyObject *self, PyObject *args);
PyObject *automation_create_source(PyObject *self, PyObject *args);
PyObject *automation_vtable_dump(PyObject *self, PyObject *args);
PyObject *pattern_read_notes(PyObject *self, PyObject *args);
PyObject *pattern_add_notes(PyObject *self, PyObject *args);
PyObject *pattern_write_notes(PyObject *self, PyObject *args);
PyObject *timeline_read_markers(PyObject *self, PyObject *args);
PyObject *timeline_add_marker(PyObject *self, PyObject *args);
PyObject *timeline_delete_marker(PyObject *self, PyObject *args);
PyObject *timeline_refresh(PyObject *self, PyObject *args);
PyObject *arrangement_read_all(PyObject *self, PyObject *args);
PyObject *arrangement_add(PyObject *self, PyObject *args);
PyObject *arrangement_clone(PyObject *self, PyObject *args);
PyObject *arrangement_delete(PyObject *self, PyObject *args);
PyObject *arrangement_set_name(PyObject *self, PyObject *args);
PyObject *arrangement_set_current(PyObject *self, PyObject *args);
PyObject *project_save_copy(PyObject *self, PyObject *args);
PyObject *mixer_remove_effect(PyObject *self, PyObject *args);

#endif
