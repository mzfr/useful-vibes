from django.db import models


class MediaType(models.TextChoices):
    MOVIE = "movie", "Movie"
    TV = "tv", "TV"
    SEASON = "season", "Season"
    EPISODE = "episode", "Episode"
    BOOK = "book", "Book"


class Source(models.TextChoices):
    TMDB = "tmdb", "TMDB"
    OPENLIBRARY = "openlibrary", "Open Library"


class Status(models.TextChoices):
    COMPLETED = "completed", "Completed"
    IN_PROGRESS = "in_progress", "In Progress"
    PLANNING = "planning", "Planning"
    PAUSED = "paused", "Paused"
    DROPPED = "dropped", "Dropped"


class MessageLevel(models.TextChoices):
    INFO = "info", "Info"
    SUCCESS = "success", "Success"
    WARNING = "warning", "Warning"
    ERROR = "error", "Error"
