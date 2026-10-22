from django.urls import path

from . import views

app_name = "imports"

urlpatterns = [
    path("settings/", views.index, name="index"),
    path("settings/tmdb", views.save_tmdb_key, name="save_tmdb_key"),
    path("settings/sources", views.save_sources_settings, name="save_sources_settings"),
    path("settings/imports/jobs", views.import_jobs, name="jobs"),
    path("settings/imports/csv-movies-shows", views.upload_csv_movies_shows, name="upload_csv_movies_shows"),
    path("settings/imports/csv-books", views.upload_csv_books, name="upload_csv_books"),
    path("settings/imports/trakt-export", views.upload_trakt_export, name="trakt_export"),
    path("settings/trakt-legacy/", views.trakt_legacy, name="trakt_legacy"),
    path("settings/imports/<int:job_id>/", views.job_detail, name="job"),
    path("settings/imports/<int:job_id>/progress", views.job_progress, name="job_progress"),
    path("settings/imports/<int:job_id>/review/<int:row_index>/search", views.review_book_search, name="review_book_search"),
    path("settings/imports/<int:job_id>/review/<int:row_index>/match", views.review_book_match, name="review_book_match"),
    path("settings/imports/<int:job_id>/review/<int:row_index>/skip", views.review_book_skip, name="review_book_skip"),
    path("settings/exports/movies-shows.csv", views.export_movies_shows, name="export_movies_shows"),
    path("settings/exports/books.csv", views.export_books, name="export_books"),
    path("settings/exports/all.json", views.export_json, name="export_json"),
]
