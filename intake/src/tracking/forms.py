from __future__ import annotations

from django import forms

from core.enums import Status


class TrackForm(forms.Form):
    """Track modal form. Used by both the full-page and HTMX endpoints."""

    status = forms.ChoiceField(choices=Status.choices, required=False)
    score = forms.IntegerField(min_value=1, max_value=10, required=False)
    progress = forms.IntegerField(min_value=0, required=False)
    start_date = forms.DateField(required=False)
    end_date = forms.DateField(required=False)
