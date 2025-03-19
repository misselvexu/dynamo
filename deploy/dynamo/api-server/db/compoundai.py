# This file is for compoundai metadata APIs
import json
from datetime import datetime, timezone
from typing import Annotated, List, Optional

import requests
from fastapi import APIRouter, Body, Depends, HTTPException, Request, responses
from nemo_microservice_logging import NemoLogger
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import asc, col, desc, select
from sqlmodel.ext.asyncio.session import AsyncSession

from datastore.common.db import get_session
from datastore.common.errors import GitError, GitProtectedFileError, check_repo_name_available
from datastore.common.git.git_client import GitClient, get_git_client
from datastore.common.git.util import get_lfs_pointer_or_raise_http_exception
from datastore.common.huggingface import RepoType, upload_file
from datastore.components.compoundai import (
    CompoundNimSchema,
    CompoundNimSchemaWithDeploymentsListSchema,
    CompoundNimSchemaWithDeploymentsSchema,
    CompoundNimUploadStatus,
    CompoundNimVersionFullSchema,
    CompoundNimVersionSchema,
    CompoundNimVersionsWithNimListSchema,
    CompoundNimVersionWithNimSchema,
    CreateCompoundNimRequest,
    CreateCompoundNimVersionRequest,
    ImageBuildStatus,
    ListQuerySchema,
    OrganizationSchema,
    ResourceType,
    TransmissionStrategy,
    UpdateCompoundNimVersionRequest,
    UserSchema,
)
from datastore.components.db_model import CompoundNim, CompoundNimVersion

API_TAG_MODELS = "compoundai"

DEFAULT_LIMIT = 3
SORTABLE_COLUMNS = {
    "created_at": col(CompoundNim.created_at),
    "update_at": col(CompoundNim.updated_at),
}

router = APIRouter()
logger = NemoLogger(__name__)


@router.get(
    "/api/v1/auth/current",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
async def login(
    request: Request,
):
    return UserSchema(
        name="compoundai", email="compoundai@nvidia.com", first_name="compound", last_name="ai"
    )


@router.get(
    "/api/v1/current_org",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
async def current_org(
    request: Request,
):
    return OrganizationSchema(
        uid="uid",
        created_at=datetime(2024, 9, 18, 12, 0, 0),
        updated_at=datetime(2024, 9, 18, 12, 0, 0),
        deleted_at=None,
        name="nvidia",
        resource_type=ResourceType.Organization,
        labels=[],
        description="CompoundAI default organization.",
    )


# GetCompoundNim is a FastAPI dependency that will perform stored model lookup.
async def compound_nim_handler(
    *,
    session: AsyncSession = Depends(get_session),
    compound_nim_name: str,
) -> CompoundNim:
    statement = select(CompoundNim).where(CompoundNim.name == compound_nim_name)
    stored_compound_nim_result = await session.exec(statement)
    stored_compound_nim = stored_compound_nim_result.first()

    if not stored_compound_nim:
        raise HTTPException(status_code=404, detail="Record not found")

    return stored_compound_nim


GetCompoundNim = Depends(compound_nim_handler)


@router.get(
    "/api/v1/bento_repositories/{compound_nim_name}",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
@router.get(
    "/api/v1/compound_nims/{compound_nim_name}",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
async def get_compound_nim(
    *,
    compound_nim: CompoundNim = GetCompoundNim,
    session: AsyncSession = Depends(get_session),
):
    statement = (
        select(CompoundNimVersion)
        .where(
            CompoundNimVersion.compound_nim_id == compound_nim.id,
        )
        .order_by(desc(CompoundNimVersion.created_at))
    )

    result = await session.exec(statement)
    compound_nims = result.all()

    latest_compound_nim_versions = await convert_compound_nim_version_model_to_schema(
        session, list(compound_nims), compound_nim
    )

    return CompoundNimSchema(
        uid=compound_nim.id,
        created_at=compound_nim.created_at,
        updated_at=compound_nim.updated_at,
        deleted_at=compound_nim.deleted_at,
        name=compound_nim.name,
        resource_type=ResourceType.CompoundNim,
        labels=[],
        description=compound_nim.description,
        latest_bento=None if not latest_compound_nim_versions else latest_compound_nim_versions[0],
        latest_bentos=latest_compound_nim_versions,
        n_bentos=len(compound_nims),
    )


@router.post(
    "/api/v1/bento_repositories",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
@router.post(
    "/api/v1/compound_nims",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
async def create_compound_nim(
    *,
    session: AsyncSession = Depends(get_session),
    git_client: GitClient = Depends(get_git_client),
    request: CreateCompoundNimRequest,
):
    """
    Create a new respository
    """
    try:
        db_compound_nim = CompoundNim.model_validate(request)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json()))  # type: ignore

    await check_repo_name_available(db_compound_nim.name, git_client)
    logger.debug("Creating repository...")

    try:
        session.add(db_compound_nim)
        await session.flush()
        await session.refresh(db_compound_nim)
    except IntegrityError as e:
        logger.error(f"Details: {str(e)}")
        await session.rollback()
        logger.error(
            f"The requested Compound NIM {db_compound_nim.name} already exists in the database"
        )
        raise HTTPException(
            status_code=422,
            detail=f"The Compound NIM {db_compound_nim.name} already exists in the database",
        )  # type: ignore
    except SQLAlchemyError as e:
        logger.error("Something went wrong with adding the repository")
        raise HTTPException(status_code=500, detail=str(e))

    await session.commit()
    logger.debug(
        f"Compound NIM {db_compound_nim.id} with name {db_compound_nim.name} saved to database"
    )

    try:
        await git_client.create_repository(db_compound_nim.name)
    except GitError as error:
        logger.error(
            f"Something went wrong creating the Compound NIM for {db_compound_nim.name}."
            " %s. Rolling back..." % error
        )
        try:
            await session.delete(db_compound_nim)
            await session.commit()
        except IntegrityError as e:
            await session.rollback()
            logger.error("Something went wrong when rolling back the database: %s\n" % e)
            raise HTTPException(status_code=500, detail=str(e))
        raise HTTPException(status_code=500, detail=str(error))

    return CompoundNimSchema(
        uid=db_compound_nim.id,
        created_at=db_compound_nim.created_at,
        updated_at=db_compound_nim.updated_at,
        deleted_at=db_compound_nim.deleted_at,
        name=db_compound_nim.name,
        resource_type=ResourceType.CompoundNim,
        labels=[],
        description=db_compound_nim.description,
        latest_bentos=None,
        latest_bento=None,
        n_bentos=0,
    )


@router.get(
    "/api/v1/bento_repositories",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
@router.get(
    "/api/v1/compound_nims",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
async def get_compound_nim_list(
    *, session: AsyncSession = Depends(get_session), query_params: ListQuerySchema = Depends()
):
    try:
        total_statement = select(func.count(col(CompoundNim.id)))
        result = await session.exec(total_statement)
        total = result.first()
        if not total:
            total = 0

        statement = select(CompoundNim)
        statement = statement.offset(query_params.start)
        statement = statement.limit(query_params.count)

        query = query_params.get_query_map()
        for k, v_list in query.items():
            if k == "sort":
                for v in v_list:
                    column, order = v.split("-")
                    if column in SORTABLE_COLUMNS and order in ["asc", "desc"]:
                        to_sort = SORTABLE_COLUMNS[column]
                        sort_by = asc(to_sort) if order == "asc" else desc(to_sort)
                        statement = statement.order_by(sort_by)

        result = await session.exec(statement)
        compound_nims = list(result.all())

        compound_nim_schemas = await convert_compound_nim_model_to_schema(session, compound_nims)

        compound_nims_with_deployments = [
            CompoundNimSchemaWithDeploymentsSchema(
                **compound_nim_schema.model_dump(), deployments=[]
            )
            for compound_nim_schema in compound_nim_schemas
        ]

        return CompoundNimSchemaWithDeploymentsListSchema(
            total=total,
            start=query_params.start,
            count=query_params.count,
            items=compound_nims_with_deployments,
        )
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json()))


async def compound_nim_version_handler(
    *, session: AsyncSession = Depends(get_session), compound_nim_name: str, version: str
) -> tuple[CompoundNimVersion, CompoundNim]:
    statement = select(CompoundNimVersion, CompoundNim).where(
        CompoundNimVersion.compound_nim_id == CompoundNim.id,
        CompoundNimVersion.version == version,
        CompoundNim.name == compound_nim_name,
    )

    result = await session.exec(statement)
    records = result.all()

    if not records:
        logger.error("No Compound NIM version record found")
        raise HTTPException(status_code=404, detail="Record not found")

    if len(records) >= 2:
        logger.error("Found multiple relations for Compound NIM version")
        raise HTTPException(
            status_code=422, detail="Found multiple relations for Compound NIM version"
        )

    return records[0]


GetCompoundNimVersion = Depends(compound_nim_version_handler)


@router.get(
    "/api/v1/bento_repositories/{compound_nim_name}/bentos/{version}",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
@router.get(
    "/api/v1/compound_nims/{compound_nim_name}/versions/{version}",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
async def get_compound_nim_version(
    *,
    compound_nim_entities: tuple[CompoundNimVersion, CompoundNim] = GetCompoundNimVersion,
    session: AsyncSession = Depends(get_session),
):
    compound_nim_version, compound_nim = compound_nim_entities
    compound_nim_version_schemas = await convert_compound_nim_version_model_to_schema(
        session, [compound_nim_version], compound_nim
    )
    compound_nim_schemas = await convert_compound_nim_model_to_schema(session, [compound_nim])

    full_schema = CompoundNimVersionFullSchema(
        **compound_nim_version_schemas[0].model_dump(), repository=compound_nim_schemas[0]
    )
    return full_schema


@router.post(
    "/api/v1/bento_repositories/{compound_nim_name}/bentos",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
@router.post(
    "/api/v1/compound_nims/{compound_nim_name}/versions",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
async def create_compound_nim_version(
    request: CreateCompoundNimVersionRequest,
    compound_nim: CompoundNim = GetCompoundNim,
    session: AsyncSession = Depends(get_session),
):
    """
    Create a new nim
    """
    try:
        # Create without validation
        db_compound_nim_version = CompoundNimVersion(
            **request.model_dump(),
            compound_nim_id=compound_nim.id,
            upload_status=CompoundNimUploadStatus.Pending,
            image_build_status=ImageBuildStatus.Pending,
        )
        CompoundNimVersion.model_validate(db_compound_nim_version)
        tag = f"{compound_nim.name}:{db_compound_nim_version.version}"
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json()))  # type: ignore
    except BaseException as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json()))  # type: ignore

    try:
        session.add(db_compound_nim_version)
        await session.flush()
        await session.refresh(db_compound_nim_version)
    except IntegrityError as e:
        logger.error(f"Details: {str(e)}")
        await session.rollback()

        logger.error(f"The Compound NIM {tag} already exists")
        raise HTTPException(
            status_code=422,
            detail=f"The Compound NIM version {tag} already exists",
        )  # type: ignore
    except SQLAlchemyError as e:
        logger.error("Something went wrong with adding the Compound NIM")
        raise HTTPException(status_code=500, detail=str(e))

    logger.debug(f"Commiting {compound_nim.name}:{db_compound_nim_version.version} to database")
    await session.commit()

    schema = await convert_compound_nim_version_model_to_schema(session, [db_compound_nim_version])
    return schema[0]


@router.get(
    "/api/v1/bento_repositories/{compound_nim_name}/bentos",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
@router.get(
    "/api/v1/compound_nims/{compound_nim_name}/versions",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
async def get_compound_nim_versions(
    *,
    compound_nim: CompoundNim = GetCompoundNim,
    session: AsyncSession = Depends(get_session),
    query_params: ListQuerySchema = Depends(),
):
    compound_nim_schemas = await convert_compound_nim_model_to_schema(session, [compound_nim])
    compound_nim_schema = compound_nim_schemas[0]

    total_statement = (
        select(CompoundNimVersion)
        .where(
            CompoundNimVersion.compound_nim_id == compound_nim.id,
        )
        .order_by(desc(CompoundNimVersion.created_at))
    )

    result = await session.exec(total_statement)
    compound_nim_versions = result.all()
    total = len(compound_nim_versions)

    statement = total_statement.limit(query_params.count)
    result = await session.exec(statement)
    compound_nim_versions = list(result.all())

    compound_nim_version_schemas = await convert_compound_nim_version_model_to_schema(
        session, compound_nim_versions, compound_nim
    )

    items = [
        CompoundNimVersionWithNimSchema(**version.model_dump(), repository=compound_nim_schema)
        for version in compound_nim_version_schemas
    ]

    return CompoundNimVersionsWithNimListSchema(
        total=total, count=query_params.count, start=query_params.start, items=items
    )


@router.patch(
    "/api/v1/bento_repositories/{compound_nim_name}/bentos/{version}",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
@router.patch(
    "/api/v1/compound_nims/{compound_nim_name}/versions/{version}",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
async def update_compound_nim_version(
    *,
    compound_nim_entities: tuple[CompoundNimVersion, CompoundNim] = GetCompoundNimVersion,
    request: UpdateCompoundNimVersionRequest,
    session: AsyncSession = Depends(get_session),
):
    compound_nim_version, _ = compound_nim_entities
    compound_nim_version.manifest = request.manifest.model_dump()

    try:
        session.add(compound_nim_version)
        await session.flush()
        await session.refresh(compound_nim_version)
    except SQLAlchemyError as e:
        logger.error("Something went wrong with adding the Compound NIM")
        raise HTTPException(status_code=500, detail=str(e))

    logger.debug("Updating compound Compound NIM")
    await session.commit()

    schema = await convert_compound_nim_version_model_to_schema(session, [compound_nim_version])
    return schema[0]


@router.put(
    "/api/v1/bento_repositories/{compound_nim_name}/bentos/{version}/upload",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
@router.put(
    "/api/v1/compound_nims/{compound_nim_name}/versions/{version}/upload",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
def upload_compound_nim_version(
    *,
    compound_nim_entities: tuple[CompoundNimVersion, CompoundNim] = GetCompoundNimVersion,
    version: str,
    file: Annotated[bytes, Body()],
):
    _, compound_nim = compound_nim_entities
    filepath = generate_file_path(version)

    try:
        resp = upload_file(
            filepath=filepath,
            file=file,
            repo_name=compound_nim.name,
            repo_type=RepoType.DATASET,
            commit_message=None,
        )
        logger.debug(f"File {filepath} successfully uploaded to {compound_nim.name}")
        return resp
    except GitProtectedFileError as e:
        logger.error(f"Invalid file upload to Compound NIM {compound_nim.name} %s" % e)
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as err:
        logger.error("Something went wrong during the file upload %s" % err)
        raise HTTPException(status_code=500, detail=str(err))


def generate_file_path(version) -> str:
    return f"compoundai-{version}"


@router.get(
    "/api/v1/bento_repositories/{compound_nim_name}/bentos/{version}/download",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
@router.get(
    "/api/v1/compound_nims/{compound_nim_name}/versions/{version}/download",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
async def download_compound_nim_version(
    *,
    git_client: GitClient = Depends(get_git_client),
    compound_nim_entities: tuple[CompoundNimVersion, CompoundNim] = GetCompoundNimVersion,
    version: str,
):
    _, compound_nim = compound_nim_entities
    filepath = generate_file_path(version)

    content = await get_lfs_pointer_or_raise_http_exception(
        git_client, compound_nim.name, "main", filepath
    )

    response = requests.get(content.url)

    if response.status_code == 200:
        return responses.StreamingResponse(
            response.iter_content(chunk_size=8192), media_type=response.headers.get("Content-Type")
        )
    else:
        raise Exception(f"Failed to retrieve file: {response.status_code} - {response.text}")


@router.patch(
    "/api/v1/bento_repositories/{compound_nim_name}/bentos/{version}/start_upload",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
@router.patch(
    "/api/v1/compound_nims/{compound_nim_name}/versions/{version}/start_upload",
    responses={
        200: {"description": "Successful Response"},
        422: {"description": "Validation Error"},
    },
    tags=[API_TAG_MODELS],
    include_in_schema=False,
)
async def start_compound_nim_version_upload(
    *,
    compound_nim_entities: tuple[CompoundNimVersion, CompoundNim] = GetCompoundNimVersion,
    session: AsyncSession = Depends(get_session),
):
    compound_nim_version, _ = compound_nim_entities
    compound_nim_version.upload_status = CompoundNimUploadStatus.Uploading

    try:
        session.add(compound_nim_version)
        await session.flush()
        await session.refresh(compound_nim_version)
    except SQLAlchemyError as e:
        logger.error("Something went wrong with adding the Compound NIM")
        raise HTTPException(status_code=500, detail=str(e))

    logger.debug("Setting Compound NIM upload status to Uploading.")
    await session.commit()

    schema = await convert_compound_nim_version_model_to_schema(session, [compound_nim_version])
    return schema[0]


"""
    DB to Schema Converters
"""


async def convert_compound_nim_model_to_schema(
    session: AsyncSession, entities: List[CompoundNim]
) -> List[CompoundNimSchema]:
    compound_nim_schemas = []
    for entity in entities:
        try:
            statement = (
                select(CompoundNimVersion)
                .where(
                    CompoundNimVersion.compound_nim_id == entity.id,
                )
                .order_by(desc(CompoundNimVersion.created_at))
                .limit(DEFAULT_LIMIT)
            )

            total_statement = select(func.count(col(CompoundNimVersion.id))).where(
                CompoundNimVersion.compound_nim_id == entity.id
            )
            result = await session.exec(total_statement)
            total = result.first()
            if not total:
                total = 0

            result = await session.exec(statement)
            compound_nim_versions = list(result.all())
            compound_nim_version_schemas = await convert_compound_nim_version_model_to_schema(
                session, compound_nim_versions, entity
            )

            compound_nim_schemas.append(
                CompoundNimSchema(
                    uid=entity.id,
                    created_at=entity.created_at.replace(tzinfo=timezone.utc),
                    updated_at=entity.updated_at.replace(tzinfo=timezone.utc),
                    deleted_at=(
                        None
                        if not entity.deleted_at
                        else entity.deleted_at.replace(tzinfo=timezone.utc)
                    ),
                    name=entity.name,
                    resource_type=ResourceType.CompoundNim,
                    labels=[],
                    latest_bento=(
                        None
                        if not compound_nim_version_schemas
                        else compound_nim_version_schemas[0]
                    ),
                    latest_bentos=compound_nim_version_schemas,
                    n_bentos=total,
                    description=entity.description,
                )
            )
        except SQLAlchemyError as e:
            logger.error("Something went wrong with getting associated Compound NIM versions")
            raise HTTPException(status_code=500, detail=str(e))

    return compound_nim_schemas


async def convert_compound_nim_version_model_to_schema(
    session: AsyncSession,
    entities: List[CompoundNimVersion],
    compound_nim: Optional[CompoundNim] = None,
) -> List[CompoundNimVersionSchema]:
    compound_nim_version_schemas = []
    for entity in entities:
        if not compound_nim:
            statement = select(CompoundNim).where(CompoundNim.id == entity.compound_nim_id)
            results = await session.exec(statement)
            compound_nim = results.first()

        if compound_nim:
            compound_nim_version_schema = CompoundNimVersionSchema(
                description=entity.description,
                version=entity.version,
                image_build_status=entity.image_build_status,
                upload_status=str(entity.upload_status.value),
                upload_finished_reason=entity.upload_finished_reason,
                uid=entity.id,
                name=compound_nim.name,
                created_at=entity.created_at.replace(tzinfo=timezone.utc),
                resource_type=ResourceType.CompoundNimVersion,
                labels=[],
                manifest=entity.manifest,
                updated_at=entity.updated_at.replace(tzinfo=timezone.utc),
                bento_repository_uid=compound_nim.id,
                upload_started_at=(
                    entity.upload_started_at.replace(tzinfo=timezone.utc)
                    if entity.upload_started_at
                    else None
                ),
                upload_finished_at=(
                    entity.upload_finished_at.replace(tzinfo=timezone.utc)
                    if entity.upload_finished_at
                    else None
                ),
                transmission_strategy=TransmissionStrategy.Proxy,
                build_at=entity.build_at.replace(tzinfo=timezone.utc),
            )

            compound_nim_version_schemas.append(compound_nim_version_schema)
        else:
            raise HTTPException(
                status_code=500, detail="Failed to find related Compound NIM"
            )  # Should never happen

    return compound_nim_version_schemas
