# Copyright (C) 2021-2023 Intel Corporation
#
# SPDX-License-Identifier: MIT

import os
import boto3
import functools
import json

from abc import ABC, abstractmethod, abstractproperty
from enum import Enum
from io import BytesIO
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError
from botocore.handlers import disable_signing

from azure.storage.blob import BlobServiceClient, ContainerClient
from azure.core.exceptions import ResourceExistsError, HttpResponseError
from azure.storage.blob import PublicAccess

from google.cloud import storage
from google.cloud.exceptions import NotFound as GoogleCloudNotFound, Forbidden as GoogleCloudForbidden

from cvat.apps.engine.log import slogger
from cvat.apps.engine.models import CredentialsTypeChoice, CloudProviderChoice

from typing import Optional

class Status(str, Enum):
    AVAILABLE = 'AVAILABLE'
    NOT_FOUND = 'NOT_FOUND'
    FORBIDDEN = 'FORBIDDEN'

    @classmethod
    def choices(cls):
        return tuple((x.value, x.name) for x in cls)

    def __str__(self):
        return self.value

class Permissions(str, Enum):
    READ = 'read'
    WRITE = 'write'

    @classmethod
    def all(cls):
        return {i.value for i in cls}


def validate_bucket_status(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            res = func(self, *args, **kwargs)
        except Exception as ex:
            # check that cloud storage exists
            storage_status = self.get_status() if self is not None else None
            if storage_status == Status.FORBIDDEN:
                raise PermissionDenied('The resource {} is no longer available. Access forbidden.'.format(self.name))
            elif storage_status == Status.NOT_FOUND:
                raise NotFound('The resource {} not found. It may have been deleted.'.format(self.name))
            elif storage_status == Status.AVAILABLE:
                raise
            raise ValidationError(str(ex))
        return res
    return wrapper

def validate_file_status(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            res = func(self, *args, **kwargs)
        except Exception as ex:
            storage_status = self.get_status() if self is not None else None
            if storage_status == Status.AVAILABLE:
                key = args[0]
                file_status = self.get_file_status(key)
                if file_status == Status.NOT_FOUND:
                    raise NotFound("The file '{}' not found on the cloud storage '{}'".format(key, self.name))
                elif file_status == Status.FORBIDDEN:
                    raise PermissionDenied("Access to the file '{}' on the '{}' cloud storage is denied".format(key, self.name))
                raise ValidationError(str(ex))
            else:
                raise
        return res
    return wrapper

class _CloudStorage(ABC):

    def __init__(self):
        self._files = []

    @abstractproperty
    def name(self):
        pass

    @abstractmethod
    def create(self):
        pass

    @abstractmethod
    def _head_file(self, key):
        pass

    @abstractmethod
    def _head(self):
        pass

    @abstractmethod
    def get_status(self):
        pass

    @abstractmethod
    def get_file_status(self, key):
        pass

    @abstractmethod
    def get_file_last_modified(self, key):
        pass

    @abstractmethod
    def initialize_content(self):
        pass

    @abstractmethod
    def download_fileobj(self, key):
        pass

    def download_file(self, key, path):
        file_obj = self.download_fileobj(key)
        if isinstance(file_obj, BytesIO):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(file_obj.getvalue())
        else:
            raise NotImplementedError("Unsupported type {} was found".format(type(file_obj)))

    @abstractmethod
    def upload_fileobj(self, file_obj, file_name):
        pass

    @abstractmethod
    def upload_file(self, file_path, file_name=None):
        pass

    def __contains__(self, file_name):
        return file_name in (item['name'] for item in self._files)

    def __len__(self):
        return len(self._files)

    @property
    def content(self):
        return list(map(lambda x: x['name'] , self._files))

    @abstractproperty
    def supported_actions(self):
        pass

    @property
    def read_access(self):
        return Permissions.READ in self.access

    @property
    def write_access(self):
        return Permissions.WRITE in self.access

def get_cloud_storage_instance(cloud_provider, resource, credentials, specific_attributes=None, endpoint=None):
    instance = None
    if cloud_provider == CloudProviderChoice.AWS_S3:
        instance = AWS_S3(
            bucket=resource,
            access_key_id=credentials.key,
            secret_key=credentials.secret_key,
            session_token=credentials.session_token,
            region=specific_attributes.get('region'),
            endpoint_url=specific_attributes.get('endpoint_url'),
        )
    elif cloud_provider == CloudProviderChoice.AZURE_CONTAINER:
        instance = AzureBlobContainer(
            container=resource,
            account_name=credentials.account_name,
            sas_token=credentials.session_token,
            connection_string=credentials.connection_string
        )
    elif cloud_provider == CloudProviderChoice.GOOGLE_CLOUD_STORAGE:
        instance = GoogleCloudStorage(
            bucket_name=resource,
            service_account_json=credentials.key_file_path,
            anonymous_access = credentials.credentials_type == CredentialsTypeChoice.ANONYMOUS_ACCESS,
            prefix=specific_attributes.get('prefix'),
            location=specific_attributes.get('location'),
            project=specific_attributes.get('project')
        )
    else:
        raise NotImplementedError()
    return instance

class AWS_S3(_CloudStorage):
    transfer_config = {
        'max_io_queue': 10,
    }

    class Effect(str, Enum):
        ALLOW = 'Allow'
        DENY = 'Deny'


    def __init__(self,
                bucket,
                region,
                access_key_id=None,
                secret_key=None,
                session_token=None,
                endpoint_url=None):
        super().__init__()
        if all([access_key_id, secret_key, session_token]):
            self._s3 = boto3.resource(
                's3',
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key,
                aws_session_token=session_token,
                region_name=region,
                endpoint_url=endpoint_url
            )
        elif access_key_id and secret_key:
            self._s3 = boto3.resource(
                's3',
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key,
                region_name=region,
                endpoint_url=endpoint_url
            )
        elif any([access_key_id, secret_key, session_token]):
            raise Exception('Insufficient data for authorization')
        # anonymous access
        if not any([access_key_id, secret_key, session_token]):
            self._s3 = boto3.resource('s3', region_name=region, endpoint_url=endpoint_url)
            self._s3.meta.client.meta.events.register('choose-signer.s3.*', disable_signing)
        self._client_s3 = self._s3.meta.client
        self._bucket = self._s3.Bucket(bucket)
        self.region = region

    @property
    def bucket(self):
        return self._bucket

    @property
    def name(self):
        return self._bucket.name

    def _head(self):
        return self._client_s3.head_bucket(Bucket=self.name)

    def _head_file(self, key):
        return self._client_s3.head_object(Bucket=self.name, Key=key)

    def get_status(self):
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#S3.Client.head_object
        # return only 3 codes: 200, 403, 404
        try:
            self._head()
            return Status.AVAILABLE
        except ClientError as ex:
            code = ex.response['Error']['Code']
            if code == '403':
                return Status.FORBIDDEN
            else:
                return Status.NOT_FOUND

    def get_file_status(self, key):
        try:
            self._head_file(key)
            return Status.AVAILABLE
        except ClientError as ex:
            code = ex.response['Error']['Code']
            if code == '403':
                return Status.FORBIDDEN
            else:
                return Status.NOT_FOUND

    @validate_file_status
    @validate_bucket_status
    def get_file_last_modified(self, key):
        return self._head_file(key).get('LastModified')

    @validate_bucket_status
    def upload_fileobj(self, file_obj, file_name):
        self._bucket.upload_fileobj(
            Fileobj=file_obj,
            Key=file_name,
            Config=TransferConfig(max_io_queue=self.transfer_config['max_io_queue'])
        )

    @validate_bucket_status
    def upload_file(self, file_path, file_name=None):
        if not file_name:
            file_name = os.path.basename(file_path)
        try:
            self._bucket.upload_file(
                file_path,
                file_name,
                Config=TransferConfig(max_io_queue=self.transfer_config['max_io_queue'])
            )
        except ClientError as ex:
            msg = str(ex)
            slogger.glob.error(msg)
            raise Exception(msg)

    def initialize_content(self):
        files = self._bucket.objects.all()
        self._files = [{
            'name': item.key,
        } for item in files]

    @validate_file_status
    @validate_bucket_status
    def download_fileobj(self, key):
        buf = BytesIO()
        self.bucket.download_fileobj(
            Key=key,
            Fileobj=buf,
            Config=TransferConfig(max_io_queue=self.transfer_config['max_io_queue'])
        )
        buf.seek(0)
        return buf

    def create(self):
        try:
            responce = self._bucket.create(
                ACL='private',
                CreateBucketConfiguration={
                    'LocationConstraint': self.region,
                },
                ObjectLockEnabledForBucket=False
            )
            slogger.glob.info(
                'Bucket {} has been created on {} region'.format(
                    self.name,
                    responce['Location']
                ))
        except Exception as ex:
            msg = str(ex)
            slogger.glob.info(msg)
            raise Exception(msg)

    def delete_file(self, file_name: str):
        try:
            self._client_s3.delete_object(Bucket=self.name, Key=file_name)
        except Exception as ex:
            msg = str(ex)
            slogger.glob.info(msg)
            raise

    @property
    def supported_actions(self):
        allowed_actions = set()
        try:
            bucket_policy = self._bucket.Policy().policy
        except ClientError as ex:
            if 'NoSuchBucketPolicy' in str(ex):
                return Permissions.all()
            else:
                raise Exception(str(ex))
        bucket_policy = json.loads(bucket_policy) if isinstance(bucket_policy, str) else bucket_policy
        for statement in bucket_policy['Statement']:
            effect = statement.get('Effect') # Allow | Deny
            actions = statement.get('Action', set())
            if effect == self.Effect.ALLOW:
                allowed_actions.update(actions)
        access = {
            's3:GetObject': Permissions.READ,
            's3:PutObject': Permissions.WRITE,
        }
        allowed_actions = Permissions.all() & {access.get(i) for i in allowed_actions}

        return allowed_actions

class AzureBlobContainer(_CloudStorage):
    MAX_CONCURRENCY = 3


    class Effect:
        pass

    def __init__(
        self,
        container: str,
        account_name: Optional[str] = None,
        sas_token: Optional[str] = None,
        connection_string: Optional[str] = None
    ):
        super().__init__()
        self._account_name = account_name
        if connection_string:
            self._blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        elif sas_token:
            self._blob_service_client = BlobServiceClient(account_url=self.account_url, credential=sas_token)
        else:
            self._blob_service_client = BlobServiceClient(account_url=self.account_url)
        self._container_client = self._blob_service_client.get_container_client(container)

    @property
    def container(self) -> ContainerClient:
        return self._container_client

    @property
    def name(self) -> str:
        return self._container_client.container_name

    @property
    def account_url(self) -> Optional[str]:
        if self._account_name:
            return "{}.blob.core.windows.net".format(self._account_name)
        return None

    def create(self):
        try:
            self._container_client.create_container(
               metadata={
                   'type' : 'created by CVAT',
               },
               public_access=PublicAccess.OFF
            )
        except ResourceExistsError:
            msg = f"{self._container_client.container_name} already exists"
            slogger.glob.info(msg)
            raise Exception(msg)

    def _head(self):
        return self._container_client.get_container_properties()

    def _head_file(self, key):
        blob_client = self.container.get_blob_client(key)
        return blob_client.get_blob_properties()

    @validate_file_status
    @validate_bucket_status
    def get_file_last_modified(self, key):
        return self._head_file(key).last_modified

    def get_status(self):
        try:
            self._head()
            return Status.AVAILABLE
        except HttpResponseError as ex:
            if  ex.status_code == 403:
                return Status.FORBIDDEN
            else:
                return Status.NOT_FOUND

    def get_file_status(self, key):
        try:
            self._head_file(key)
            return Status.AVAILABLE
        except HttpResponseError as ex:
            if  ex.status_code == 403:
                return Status.FORBIDDEN
            else:
                return Status.NOT_FOUND

    @validate_bucket_status
    def upload_fileobj(self, file_obj, file_name):
        self._container_client.upload_blob(name=file_name, data=file_obj)

    def upload_file(self, file_path, file_name=None):
        if not file_name:
            file_name = os.path.basename(file_path)
        with open(file_path, 'rb') as f:
            self.upload_fileobj(f.read(), file_name)

    # TODO:
    # def multipart_upload(self, file_obj):
    #     pass

    def initialize_content(self):
        files = self._container_client.list_blobs()
        self._files = [{
            'name': item.name
        } for item in files]

    @validate_file_status
    @validate_bucket_status
    def download_fileobj(self, key):
        buf = BytesIO()
        storage_stream_downloader = self._container_client.download_blob(
            blob=key,
            offset=None,
            length=None,
        )
        storage_stream_downloader.download_to_stream(buf, max_concurrency=self.MAX_CONCURRENCY)
        buf.seek(0)
        return buf

    @property
    def supported_actions(self):
        pass

class GOOGLE_DRIVE(_CloudStorage):
    pass

def _define_gcs_status(func):
    def wrapper(self, key=None):
        try:
            if not key:
                func(self)
            else:
                func(self, key)
            return Status.AVAILABLE
        except GoogleCloudNotFound:
            return Status.NOT_FOUND
        except GoogleCloudForbidden:
            return Status.FORBIDDEN
    return wrapper

class GoogleCloudStorage(_CloudStorage):

    class Effect:
        pass

    def __init__(self, bucket_name, prefix=None, service_account_json=None, anonymous_access=False, project=None, location=None):
        super().__init__()
        if service_account_json:
            self._storage_client = storage.Client.from_service_account_json(service_account_json)
        elif anonymous_access:
            self._storage_client = storage.Client.create_anonymous_client()
        else:
            # If no credentials were provided when constructing the client, the
            # client library will look for credentials in the environment.
            self._storage_client = storage.Client()

        self._bucket = self._storage_client.bucket(bucket_name, user_project=project)
        self._bucket_location = location
        self._prefix = prefix

    @property
    def bucket(self):
        return self._bucket

    @property
    def name(self):
        return self._bucket.name

    def _head(self):
        return self._storage_client.get_bucket(bucket_or_name=self.name)

    def _head_file(self, key):
        blob = self.bucket.blob(key)
        return self._storage_client._get_resource(blob.path)

    @_define_gcs_status
    def get_status(self):
        self._head()

    @_define_gcs_status
    def get_file_status(self, key):
        self._head_file(key)

    def initialize_content(self):
        self._files = [
            {
                'name': blob.name
            }
            for blob in self._storage_client.list_blobs(
                self.bucket, prefix=self._prefix
            )
        ]

    @validate_file_status
    @validate_bucket_status
    def download_fileobj(self, key):
        buf = BytesIO()
        blob = self.bucket.blob(key)
        self._storage_client.download_blob_to_file(blob, buf)
        buf.seek(0)
        return buf

    @validate_bucket_status
    def upload_fileobj(self, file_obj, file_name):
        self.bucket.blob(file_name).upload_from_file(file_obj)

    @validate_bucket_status
    def upload_file(self, file_path, file_name=None):
        if not file_name:
            file_name = os.path.basename(file_path)
        self.bucket.blob(file_name).upload_from_filename(file_path)

    def create(self):
        try:
            self._bucket = self._storage_client.create_bucket(
                self.bucket,
                location=self._bucket_location
            )
            slogger.glob.info(
                'Bucket {} has been created at {} region for {}'.format(
                    self.name,
                    self.bucket.location,
                    self.bucket.user_project,
                ))
        except Exception as ex:
            msg = str(ex)
            slogger.glob.info(msg)
            raise Exception(msg)

    @validate_file_status
    @validate_bucket_status
    def get_file_last_modified(self, key):
        blob = self.bucket.blob(key)
        blob.reload()
        return blob.updated

    @property
    def supported_actions(self):
        pass

class Credentials:
    __slots__ = ('key', 'secret_key', 'session_token', 'account_name', 'key_file_path', 'credentials_type', 'connection_string')

    def __init__(self, **credentials):
        self.key = credentials.get('key', '')
        self.secret_key = credentials.get('secret_key', '')
        self.session_token = credentials.get('session_token', '')
        self.account_name = credentials.get('account_name', '')
        self.key_file_path = credentials.get('key_file_path', '')
        self.credentials_type = credentials.get('credentials_type', None)
        self.connection_string = credentials.get('connection_string', None)

    def convert_to_db(self):
        converted_credentials = {
            CredentialsTypeChoice.KEY_SECRET_KEY_PAIR : \
                " ".join([self.key, self.secret_key]),
            CredentialsTypeChoice.ACCOUNT_NAME_TOKEN_PAIR : " ".join([self.account_name, self.session_token]),
            CredentialsTypeChoice.KEY_FILE_PATH: self.key_file_path,
            CredentialsTypeChoice.ANONYMOUS_ACCESS: "" if not self.account_name else self.account_name,
            CredentialsTypeChoice.CONNECTION_STRING: self.connection_string,
        }
        return converted_credentials[self.credentials_type]

    def convert_from_db(self, credentials):
        self.credentials_type = credentials.get('type')
        if self.credentials_type == CredentialsTypeChoice.KEY_SECRET_KEY_PAIR:
            self.key, self.secret_key = credentials.get('value').split()
        elif self.credentials_type == CredentialsTypeChoice.ACCOUNT_NAME_TOKEN_PAIR:
            self.account_name, self.session_token = credentials.get('value').split()
        elif self.credentials_type == CredentialsTypeChoice.ANONYMOUS_ACCESS:
            # account_name will be in [some_value, '']
            self.account_name = credentials.get('value')
        elif self.credentials_type == CredentialsTypeChoice.KEY_FILE_PATH:
            self.key_file_path = credentials.get('value')
        elif self.credentials_type == CredentialsTypeChoice.CONNECTION_STRING:
            self.connection_string = credentials.get('value')
        else:
            raise NotImplementedError('Found {} not supported credentials type'.format(self.credentials_type))

    def reset(self, exclusion):
        for i in set(self.__slots__) - exclusion - {'credentials_type'}:
            self.__setattr__(i, '')

    def mapping_with_new_values(self, credentials):
        self.credentials_type = credentials.get('credentials_type', self.credentials_type)
        if self.credentials_type == CredentialsTypeChoice.ANONYMOUS_ACCESS:
            self.reset(exclusion={'account_name'})
            self.account_name = credentials.get('account_name', self.account_name)
        elif self.credentials_type == CredentialsTypeChoice.KEY_SECRET_KEY_PAIR:
            self.reset(exclusion={'key', 'secret_key'})
            self.key = credentials.get('key', self.key)
            self.secret_key = credentials.get('secret_key', self.secret_key)
        elif self.credentials_type == CredentialsTypeChoice.ACCOUNT_NAME_TOKEN_PAIR:
            self.reset(exclusion={'session_token', 'account_name'})
            self.session_token = credentials.get('session_token', self.session_token)
            self.account_name = credentials.get('account_name', self.account_name)
        elif self.credentials_type == CredentialsTypeChoice.KEY_FILE_PATH:
            self.reset(exclusion={'key_file_path'})
            self.key_file_path = credentials.get('key_file_path', self.key_file_path)
        elif self.credentials_type == CredentialsTypeChoice.CONNECTION_STRING:
            self.reset(exclusion={'connection_string'})
            self.connection_string = credentials.get('connection_string', self.connection_string)
        else:
            raise NotImplementedError('Mapping credentials: unsupported credentials type')


    def values(self):
        return [self.key, self.secret_key, self.session_token, self.account_name, self.key_file_path]

def db_storage_to_storage_instance(db_storage):
    credentials = Credentials()
    credentials.convert_from_db({
        'type': db_storage.credentials_type,
        'value': db_storage.credentials,
    })
    details = {
        'resource': db_storage.resource,
        'credentials': credentials,
        'specific_attributes': db_storage.get_specific_attributes()
    }
    return get_cloud_storage_instance(cloud_provider=db_storage.provider_type, **details)
