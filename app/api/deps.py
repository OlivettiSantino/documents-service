"""Dependency providers.

Collaborators are built once in the lifespan (the composition root) and stored
on ``app.state``; these functions just hand them to the endpoints.
"""

from fastapi import Request

from app.config import Settings
from app.ports import DocumentRepositoryPort, ExtractorClientPort
from app.services.document_service import DocumentService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_document_service(request: Request) -> DocumentService:
    return request.app.state.document_service


def get_repository(request: Request) -> DocumentRepositoryPort:
    return request.app.state.repository


def get_extractor(request: Request) -> ExtractorClientPort:
    return request.app.state.extractor
