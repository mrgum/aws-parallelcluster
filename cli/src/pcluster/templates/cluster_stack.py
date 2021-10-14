# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.

# pylint: disable=too-many-lines

#
# This module contains all the classes required to convert a Cluster into a CFN template by using CDK.
#
import json
from collections import namedtuple
from datetime import datetime
from typing import Dict, List, Union

from aws_cdk import aws_cloudformation as cfn
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_efs as efs
from aws_cdk import aws_fsx as fsx
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as awslambda
from aws_cdk import aws_logs as logs
from aws_cdk.core import (
    CfnCustomResource,
    CfnOutput,
    CfnParameter,
    CfnResource,
    CfnStack,
    CfnTag,
    Construct,
    CustomResource,
    Fn,
    Stack,
)

from pcluster.aws.aws_api import AWSApi
from pcluster.config.cluster_config import (
    AwsBatchClusterConfig,
    CapacityType,
    SharedEbs,
    SharedEfs,
    SharedFsx,
    SharedStorageType,
    SlurmClusterConfig,
)
from pcluster.constants import (
    CW_LOG_GROUP_NAME_PREFIX,
    CW_LOGS_CFN_PARAM_NAME,
    OS_MAPPING,
    PCLUSTER_CLUSTER_NAME_TAG,
    PCLUSTER_QUEUE_NAME_TAG,
    PCLUSTER_S3_ARTIFACTS_DICT,
)
from pcluster.models.s3_bucket import S3Bucket
from pcluster.templates.awsbatch_builder import AwsBatchConstruct
from pcluster.templates.cdk_builder_utils import (
    ComputeNodeIamResources,
    HeadNodeIamResources,
    PclusterLambdaConstruct,
    add_lambda_cfn_role,
    apply_permissions_boundary,
    convert_deletion_policy,
    create_hash_suffix,
    get_block_device_mappings,
    get_cloud_watch_logs_policy_statement,
    get_cloud_watch_logs_retention_days,
    get_common_user_data_env,
    get_custom_tags,
    get_default_instance_tags,
    get_default_volume_tags,
    get_log_group_deletion_policy,
    get_queue_security_groups_full,
    get_shared_storage_ids_by_type,
    get_shared_storage_options_by_type,
    get_user_data_content,
)
from pcluster.templates.cw_dashboard_builder import CWDashboardConstruct
from pcluster.templates.slurm_builder import SlurmConstruct
from pcluster.utils import get_attr, join_shell_args

StorageInfo = namedtuple("StorageInfo", ["id", "config"])


class ClusterCdkStack(Stack):
    """Create the CloudFormation stack template for the Cluster."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        stack_name: str,
        cluster_config: Union[SlurmClusterConfig, AwsBatchClusterConfig],
        bucket: S3Bucket,
        log_group_name=None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._stack_name = stack_name
        self.config = cluster_config
        self.bucket = bucket
        self.timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        if self.config.is_cw_logging_enabled:
            if log_group_name:
                # pcluster update keep the log group,
                # It has to be passed in order to avoid the change of log group name because of the suffix.
                self.log_group_name = log_group_name
            else:
                # pcluster create create a log group with timestamp suffix
                timestamp = f"{datetime.utcnow().strftime('%Y%m%d%H%M')}"
                self.log_group_name = f"{CW_LOG_GROUP_NAME_PREFIX}{self.stack_name}-{timestamp}"

        self.shared_storage_mappings = {storage_type: [] for storage_type in SharedStorageType}
        self.shared_storage_options = {storage_type: "" for storage_type in SharedStorageType}
        self.shared_storage_attributes = {storage_type: {} for storage_type in SharedStorageType}

        self._add_parameters()
        self._add_resources()
        self._add_outputs()

        try:
            apply_permissions_boundary(cluster_config.iam.permissions_boundary, self)
        except AttributeError:
            pass

    # -- Utility methods --------------------------------------------------------------------------------------------- #

    def _stack_unique_id(self):
        return Fn.select(2, Fn.split("/", self.stack_id))

    def _build_resource_path(self):
        return self.stack_id

    def _get_head_node_security_groups(self):
        """Return the security groups to be used for the head node, created by us OR provided by the user."""
        return self.config.head_node.networking.security_groups or [self._head_security_group.ref]

    def _get_head_node_security_groups_full(self):
        """Return full security groups to be used for the head node, default plus additional ones."""
        head_node_group_set = self._get_head_node_security_groups()
        # Additional security groups
        if self.config.head_node.networking.additional_security_groups:
            head_node_group_set.extend(self.config.head_node.networking.additional_security_groups)

        return head_node_group_set

    def _get_compute_security_groups(self):
        """Return list of security groups to be used for the compute, created by us AND provided by the user."""
        compute_group_set = self.config.compute_security_groups
        if self._compute_security_group:
            compute_group_set.append(self._compute_security_group.ref)

        return compute_group_set

    # -- Parameters -------------------------------------------------------------------------------------------------- #

    def _add_parameters(self):
        CfnParameter(
            self,
            "ClusterUser",
            description="Username to login to head node",
            default=OS_MAPPING[self.config.image.os]["user"],
        )
        CfnParameter(
            self,
            "ResourcesS3Bucket",
            description="S3 user bucket where AWS ParallelCluster resources are stored",
            default=self.bucket.name,
        )
        CfnParameter(
            self,
            "ArtifactS3RootDirectory",
            description="Root directory in S3 bucket where cluster artifacts are stored",
            default=self.bucket.artifact_directory,
        )
        CfnParameter(self, "Scheduler", default=self.config.scheduling.scheduler)
        CfnParameter(
            self,
            "ConfigVersion",
            description="Version of the original config used to generate the stack",
            default=self.config.original_config_version,
        )
        if self.config.is_cw_logging_enabled:
            CfnParameter(
                self,
                CW_LOGS_CFN_PARAM_NAME,
                description="CloudWatch Log Group associated to the cluster",
                default=self.log_group_name,
            )

    # -- Resources --------------------------------------------------------------------------------------------------- #

    def _add_resources(self):
        # Cloud Watch Logs
        self.log_group = None
        if self.config.is_cw_logging_enabled:
            self.log_group = self._add_cluster_log_group()

        self._add_iam_resources()

        # Managed security groups
        self._head_security_group, self._compute_security_group = self._add_security_groups()

        # Head Node ENI
        self._head_eni = self._add_head_eni()

        # Additional Cfn Stack
        if self.config.additional_resources:
            CfnStack(self, "AdditionalCfnStack", template_url=self.config.additional_resources)

        # Cleanup Resources Lambda Function
        cleanup_lambda_role, cleanup_lambda = self._add_cleanup_resources_lambda()

        if self.config.shared_storage:
            for storage in self.config.shared_storage:
                self._add_shared_storage(storage)

        # Compute Fleet and scheduler related resources
        self.scheduler_resources = None
        if self._condition_is_slurm():
            self.scheduler_resources = SlurmConstruct(
                scope=self,
                id="Slurm",
                stack_name=self._stack_name,
                cluster_config=self.config,
                bucket=self.bucket,
                managed_head_node_instance_role=self._managed_head_node_instance_role,
                managed_compute_instance_roles=self._managed_compute_instance_roles,
                cleanup_lambda_role=cleanup_lambda_role,  # None if provided by the user
                cleanup_lambda=cleanup_lambda,
            )
        self.compute_fleet_resources = None
        if not self._condition_is_batch():
            self.compute_fleet_resources = ComputeFleetConstruct(
                scope=self,
                id="ComputeFleet",
                cluster_config=self.config,
                log_group=self.log_group,
                cleanup_lambda=cleanup_lambda,
                cleanup_lambda_role=cleanup_lambda_role,
                compute_security_group=self._compute_security_group,
                shared_storage_mappings=self.shared_storage_mappings,
                shared_storage_options=self.shared_storage_options,
                shared_storage_attributes=self.shared_storage_attributes,
                compute_node_instance_profiles=self._compute_instance_profiles,
                cluster_hosted_zone=self.scheduler_resources.cluster_hosted_zone if self.scheduler_resources else None,
                dynamodb_table=self.scheduler_resources.dynamodb_table if self.scheduler_resources else None,
            )

        self._add_byos_substack()

        # Wait condition
        self.wait_condition, self.wait_condition_handle = self._add_wait_condition()

        # Head Node
        self.head_node_instance = self._add_head_node()

        # AWS Batch related resources
        if self._condition_is_batch():
            self.scheduler_resources = AwsBatchConstruct(
                scope=self,
                id="AwsBatch",
                stack_name=self._stack_name,
                cluster_config=self.config,
                bucket=self.bucket,
                create_lambda_roles=self._condition_create_lambda_iam_role(),
                compute_security_group=self._compute_security_group,
                shared_storage_mappings=self.shared_storage_mappings,
                shared_storage_options=self.shared_storage_options,
                head_node_instance=self.head_node_instance,
                managed_head_node_instance_role=self._managed_head_node_instance_role,  # None if provided by the user
            )

        # CloudWatch Dashboard
        if self.config.is_cw_dashboard_enabled:
            self.cloudwatch_dashboard = CWDashboardConstruct(
                scope=self,
                id="PclusterDashboard",
                stack_name=self.stack_name,
                cluster_config=self.config,
                head_node_instance=self.head_node_instance,
                shared_storage_mappings=self.shared_storage_mappings,
                cw_log_group_name=self.log_group.log_group_name if self.config.is_cw_logging_enabled else None,
            )

    def _add_iam_resources(self):
        head_node_iam_resources = HeadNodeIamResources(
            self, "HeadNodeIamResources", self.config, self.config.head_node, "HeadNode", self.bucket
        )
        self._head_node_instance_profile = head_node_iam_resources.instance_profile
        self._managed_head_node_instance_role = head_node_iam_resources.instance_role

        if not self._condition_is_batch():
            iam_resources = {}
            for queue in self.config.scheduling.queues:
                iam_resources[queue.name] = ComputeNodeIamResources(
                    self, f"ComputeNodeIamResources{queue.name}", self.config, queue, queue.name
                )

            self._compute_instance_profiles = {k: v.instance_profile for k, v in iam_resources.items()}
            self._managed_compute_instance_roles = {k: v.instance_role for k, v in iam_resources.items()}

    def _add_cluster_log_group(self):
        log_group = logs.CfnLogGroup(
            self,
            "CloudWatchLogGroup",
            log_group_name=self.log_group_name,
            retention_in_days=get_cloud_watch_logs_retention_days(self.config),
        )
        log_group.cfn_options.deletion_policy = get_log_group_deletion_policy(self.config)
        return log_group

    def _add_cleanup_resources_lambda(self):
        """Create Lambda cleanup resources function and its role."""
        cleanup_resources_lambda_role = None
        if self._condition_create_lambda_iam_role():
            s3_policy_actions = ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:ListBucket", "s3:ListBucketVersions"]

            cleanup_resources_lambda_role = add_lambda_cfn_role(
                scope=self,
                function_id="CleanupResources",
                statements=[
                    iam.PolicyStatement(
                        actions=s3_policy_actions,
                        effect=iam.Effect.ALLOW,
                        resources=[
                            self.format_arn(service="s3", resource=self.bucket.name, region="", account=""),
                            self.format_arn(
                                service="s3",
                                resource=f"{self.bucket.name}/{self.bucket.artifact_directory}/*",
                                region="",
                                account="",
                            ),
                        ],
                        sid="S3BucketPolicy",
                    ),
                    get_cloud_watch_logs_policy_statement(
                        resource=self.format_arn(service="logs", account="*", region="*", resource="*")
                    ),
                ],
            )

        cleanup_resources_lambda = PclusterLambdaConstruct(
            scope=self,
            id="CleanupResourcesFunctionConstruct",
            function_id="CleanupResources",
            bucket=self.bucket,
            config=self.config,
            execution_role=cleanup_resources_lambda_role.attr_arn
            if cleanup_resources_lambda_role
            else self.config.iam.roles.lambda_functions_role,
            handler_func="cleanup_resources",
        ).lambda_func

        CustomResource(
            self,
            "CleanupResourcesS3BucketCustomResource",
            service_token=cleanup_resources_lambda.attr_arn,
            properties={
                "ResourcesS3Bucket": self.bucket.name,
                "ArtifactS3RootDirectory": self.bucket.artifact_directory,
                "Action": "DELETE_S3_ARTIFACTS",
            },
        )

        return cleanup_resources_lambda_role, cleanup_resources_lambda

    def _add_head_eni(self):
        """Create Head Node Elastic Network Interface."""
        head_eni_group_set = self._get_head_node_security_groups_full()

        head_eni = ec2.CfnNetworkInterface(
            self,
            "HeadNodeENI",
            description="AWS ParallelCluster head node interface",
            subnet_id=self.config.head_node.networking.subnet_id,
            source_dest_check=False,
            group_set=head_eni_group_set,
        )

        elastic_ip = self.config.head_node.networking.elastic_ip
        if elastic_ip:
            # Create and associate EIP to Head Node
            if elastic_ip is True:
                allocation_id = ec2.CfnEIP(self, "HeadNodeEIP", domain="vpc").attr_allocation_id
            # Attach existing EIP
            else:
                allocation_id = AWSApi.instance().ec2.get_eip_allocation_id(elastic_ip)
            ec2.CfnEIPAssociation(self, "AssociateEIP", allocation_id=allocation_id, network_interface_id=head_eni.ref)

        return head_eni

    def _add_security_groups(self):
        """Associate security group to Head node and queues."""
        # Head Node Security Group
        head_security_group = None
        if not self.config.head_node.networking.security_groups:
            head_security_group = self._add_head_security_group()

        # Compute Security Groups
        managed_compute_security_group = None
        if any(not queue.networking.security_groups for queue in self.config.scheduling.queues):
            managed_compute_security_group = self._add_compute_security_group()

        if head_security_group and managed_compute_security_group:
            # Access to head node from compute nodes
            ec2.CfnSecurityGroupIngress(
                self,
                "HeadNodeSecurityGroupComputeIngress",
                ip_protocol="-1",
                from_port=0,
                to_port=65535,
                source_security_group_id=managed_compute_security_group.ref,
                group_id=head_security_group.ref,
            )

            # Access to compute nodes from head node
            ec2.CfnSecurityGroupIngress(
                self,
                "ComputeSecurityGroupHeadNodeIngress",
                ip_protocol="-1",
                from_port=0,
                to_port=65535,
                source_security_group_id=head_security_group.ref,
                group_id=managed_compute_security_group.ref,
            )

        return head_security_group, managed_compute_security_group

    def _add_compute_security_group(self):
        compute_security_group = ec2.CfnSecurityGroup(
            self, "ComputeSecurityGroup", group_description="Allow access to compute nodes", vpc_id=self.config.vpc_id
        )

        # ComputeSecurityGroupEgress
        # Access to other compute nodes from compute nodes
        compute_security_group_egress = ec2.CfnSecurityGroupEgress(
            self,
            "ComputeSecurityGroupEgress",
            ip_protocol="-1",
            from_port=0,
            to_port=65535,
            destination_security_group_id=compute_security_group.ref,
            group_id=compute_security_group.ref,
        )

        # ComputeSecurityGroupNormalEgress
        # Internet access from compute nodes
        ec2.CfnSecurityGroupEgress(
            self,
            "ComputeSecurityGroupNormalEgress",
            ip_protocol="-1",
            from_port=0,
            to_port=65535,
            cidr_ip="0.0.0.0/0",
            group_id=compute_security_group.ref,
        ).add_depends_on(compute_security_group_egress)

        # ComputeSecurityGroupIngress
        # Access to compute nodes from other compute nodes
        ec2.CfnSecurityGroupIngress(
            self,
            "ComputeSecurityGroupIngress",
            ip_protocol="-1",
            from_port=0,
            to_port=65535,
            source_security_group_id=compute_security_group.ref,
            group_id=compute_security_group.ref,
        )

        return compute_security_group

    def _add_head_security_group(self):
        head_security_group_ingress = [
            # SSH access
            ec2.CfnSecurityGroup.IngressProperty(
                ip_protocol="tcp", from_port=22, to_port=22, cidr_ip=self.config.head_node.ssh.allowed_ips
            )
        ]

        if self.config.is_dcv_enabled:
            head_security_group_ingress.append(
                # DCV access
                ec2.CfnSecurityGroup.IngressProperty(
                    ip_protocol="tcp",
                    from_port=self.config.head_node.dcv.port,
                    to_port=self.config.head_node.dcv.port,
                    cidr_ip=self.config.head_node.dcv.allowed_ips,
                )
            )
        return ec2.CfnSecurityGroup(
            self,
            "HeadNodeSecurityGroup",
            group_description="Enable access to the head node",
            vpc_id=self.config.vpc_id,
            security_group_ingress=head_security_group_ingress,
        )

    def _add_shared_storage(self, storage):
        """Add specific Cfn Resources to map the shared storage and store the output filesystem id."""
        storage_ids_list = self.shared_storage_mappings[storage.shared_storage_type]
        cfn_resource_id = "{0}{1}".format(storage.shared_storage_type.name, len(storage_ids_list))
        if storage.shared_storage_type == SharedStorageType.FSX:
            storage_ids_list.append(StorageInfo(self._add_fsx_storage(cfn_resource_id, storage), storage))
        elif storage.shared_storage_type == SharedStorageType.EBS:
            storage_ids_list.append(StorageInfo(self._add_ebs_volume(cfn_resource_id, storage), storage))
        elif storage.shared_storage_type == SharedStorageType.EFS:
            storage_ids_list.append(StorageInfo(self._add_efs_storage(cfn_resource_id, storage), storage))
        elif storage.shared_storage_type == SharedStorageType.RAID:
            storage_ids_list.extend(self._add_raid_volume(cfn_resource_id, storage))

    def _add_fsx_storage(self, id: str, shared_fsx: SharedFsx):
        """Add specific Cfn Resources to map the FSX storage."""
        fsx_id = shared_fsx.file_system_id
        # Initialize DNSName and MountName for existing filesystem, if any
        self.shared_storage_attributes[shared_fsx.shared_storage_type]["MountName"] = shared_fsx.existing_mount_name
        self.shared_storage_attributes[shared_fsx.shared_storage_type]["DNSName"] = shared_fsx.existing_dns_name

        if not fsx_id and shared_fsx.mount_dir:
            # Drive cache type must be set for HDD (Either "NONE" or "READ"), and must not be set for SDD (None).
            drive_cache_type = None
            if shared_fsx.fsx_storage_type == "HDD":
                if shared_fsx.drive_cache_type:
                    drive_cache_type = shared_fsx.drive_cache_type
                else:
                    drive_cache_type = "NONE"
            fsx_resource = fsx.CfnFileSystem(
                self,
                id,
                storage_capacity=shared_fsx.storage_capacity,
                lustre_configuration=fsx.CfnFileSystem.LustreConfigurationProperty(
                    deployment_type=shared_fsx.deployment_type,
                    data_compression_type=shared_fsx.data_compression_type,
                    imported_file_chunk_size=shared_fsx.imported_file_chunk_size,
                    export_path=shared_fsx.export_path,
                    import_path=shared_fsx.import_path,
                    weekly_maintenance_start_time=shared_fsx.weekly_maintenance_start_time,
                    automatic_backup_retention_days=shared_fsx.automatic_backup_retention_days,
                    copy_tags_to_backups=shared_fsx.copy_tags_to_backups,
                    daily_automatic_backup_start_time=shared_fsx.daily_automatic_backup_start_time,
                    per_unit_storage_throughput=shared_fsx.per_unit_storage_throughput,
                    auto_import_policy=shared_fsx.auto_import_policy,
                    drive_cache_type=drive_cache_type,
                ),
                backup_id=shared_fsx.backup_id,
                kms_key_id=shared_fsx.kms_key_id,
                file_system_type="LUSTRE",
                storage_type=shared_fsx.fsx_storage_type,
                subnet_ids=self.config.compute_subnet_ids,
                security_group_ids=self._get_compute_security_groups(),
                tags=[CfnTag(key="Name", value=shared_fsx.name)],
            )
            fsx_id = fsx_resource.ref
            # Get MountName for new filesystem
            # DNSName cannot be retrieved from CFN and will be generated in cookbook
            self.shared_storage_attributes[shared_fsx.shared_storage_type][
                "MountName"
            ] = fsx_resource.attr_lustre_mount_name

        # [shared_dir,fsx_fs_id,storage_capacity,fsx_kms_key_id,imported_file_chunk_size,
        # export_path,import_path,weekly_maintenance_start_time,deployment_type,
        # per_unit_storage_throughput,daily_automatic_backup_start_time,
        # automatic_backup_retention_days,copy_tags_to_backups,fsx_backup_id,
        # auto_import_policy,storage_type,drive_cache_type,existing_mount_name,existing_dns_name]",
        self.shared_storage_options[shared_fsx.shared_storage_type] = ",".join(
            str(item)
            for item in [
                shared_fsx.mount_dir,
                fsx_id,
                shared_fsx.storage_capacity or "NONE",
                shared_fsx.kms_key_id or "NONE",
                shared_fsx.imported_file_chunk_size or "NONE",
                shared_fsx.export_path or "NONE",
                shared_fsx.import_path or "NONE",
                shared_fsx.weekly_maintenance_start_time or "NONE",
                shared_fsx.deployment_type or "NONE",
                shared_fsx.per_unit_storage_throughput or "NONE",
                shared_fsx.daily_automatic_backup_start_time or "NONE",
                shared_fsx.automatic_backup_retention_days or "NONE",
                shared_fsx.copy_tags_to_backups if shared_fsx.copy_tags_to_backups is not None else "NONE",
                shared_fsx.backup_id or "NONE",
                shared_fsx.auto_import_policy or "NONE",
                shared_fsx.fsx_storage_type or "NONE",
                shared_fsx.drive_cache_type or "NONE",
                shared_fsx.existing_mount_name,
                shared_fsx.existing_dns_name,
            ]
        )

        return fsx_id

    def _add_efs_storage(self, id: str, shared_efs: SharedEfs):
        """Add specific Cfn Resources to map the EFS storage."""
        # EFS FileSystem
        efs_id = shared_efs.file_system_id
        new_file_system = efs_id is None
        if not efs_id and shared_efs.mount_dir:
            efs_resource = efs.CfnFileSystem(
                self,
                id,
                encrypted=shared_efs.encrypted,
                kms_key_id=shared_efs.kms_key_id,
                performance_mode=shared_efs.performance_mode,
                provisioned_throughput_in_mibps=shared_efs.provisioned_throughput,
                throughput_mode=shared_efs.throughput_mode,
            )
            efs_resource.tags.set_tag(key="Name", value=shared_efs.name)
            efs_id = efs_resource.ref

        checked_availability_zones = []

        # Mount Targets for Compute Fleet
        compute_subnet_ids = self.config.compute_subnet_ids
        compute_node_sgs = self._get_compute_security_groups()

        for subnet_id in compute_subnet_ids:
            self._add_efs_mount_target(
                id, efs_id, compute_node_sgs, subnet_id, checked_availability_zones, new_file_system
            )

        # Mount Target for Head Node
        self._add_efs_mount_target(
            id,
            efs_id,
            compute_node_sgs,
            self.config.head_node.networking.subnet_id,
            checked_availability_zones,
            new_file_system,
        )

        # [shared_dir,efs_fs_id,performance_mode,efs_kms_key_id,provisioned_throughput,encrypted,
        # throughput_mode,exists_valid_head_node_mt,exists_valid_compute_mt]
        self.shared_storage_options[shared_efs.shared_storage_type] = ",".join(
            str(item)
            for item in [
                shared_efs.mount_dir,
                efs_id,
                shared_efs.performance_mode or "NONE",
                shared_efs.kms_key_id or "NONE",
                shared_efs.provisioned_throughput or "NONE",
                shared_efs.encrypted if shared_efs.encrypted is not None else "NONE",
                shared_efs.throughput_mode or "NONE",
                "NONE",  # Useless
                "NONE",  # Useless
            ]
        )
        return efs_id

    def _add_efs_mount_target(
        self,
        efs_cfn_resource_id,
        file_system_id,
        security_groups,
        subnet_id,
        checked_availability_zones,
        new_file_system,
    ):
        """Create a EFS Mount Point for the file system, if not already available on the same AZ."""
        availability_zone = AWSApi.instance().ec2.get_subnet_avail_zone(subnet_id)
        if availability_zone not in checked_availability_zones:
            if new_file_system or not AWSApi.instance().efs.get_efs_mount_target_id(file_system_id, availability_zone):
                efs.CfnMountTarget(
                    self,
                    "{0}MT{1}".format(efs_cfn_resource_id, availability_zone),
                    file_system_id=file_system_id,
                    security_groups=security_groups,
                    subnet_id=subnet_id,
                )
            checked_availability_zones.append(availability_zone)

    def _add_raid_volume(self, id_prefix: str, shared_ebs: SharedEbs):
        """Add specific Cfn Resources to map the RAID EBS storage."""
        ebs_ids = []
        for index in range(shared_ebs.raid.number_of_volumes):
            ebs_ids.append(StorageInfo(self._add_cfn_volume(f"{id_prefix}Volume{index}", shared_ebs), shared_ebs))

        # [shared_dir,raid_type,num_of_raid_volumes,volume_type,volume_size,volume_iops,encrypted,
        # ebs_kms_key_id,volume_throughput]
        self.shared_storage_options[shared_ebs.shared_storage_type] = ",".join(
            str(item)
            for item in [
                shared_ebs.mount_dir,
                shared_ebs.raid.raid_type,
                shared_ebs.raid.number_of_volumes,
                shared_ebs.volume_type,
                shared_ebs.size,
                shared_ebs.iops,
                shared_ebs.encrypted if shared_ebs.encrypted is not None else "NONE",
                shared_ebs.kms_key_id or "NONE",
                shared_ebs.throughput,
            ]
        )

        return ebs_ids

    def _add_ebs_volume(self, id: str, shared_ebs: SharedEbs):
        """Add specific Cfn Resources to map the EBS storage."""
        ebs_id = shared_ebs.volume_id
        if not ebs_id and shared_ebs.mount_dir:
            ebs_id = self._add_cfn_volume(id, shared_ebs)

        # Append mount dir to list of shared dirs
        self.shared_storage_options[shared_ebs.shared_storage_type] += (
            f",{shared_ebs.mount_dir}"
            if self.shared_storage_options[shared_ebs.shared_storage_type]
            else f"{shared_ebs.mount_dir}"
        )

        return ebs_id

    def _add_cfn_volume(self, id: str, shared_ebs: SharedEbs):
        volume = ec2.CfnVolume(
            self,
            id,
            availability_zone=self.config.head_node.networking.availability_zone,
            encrypted=shared_ebs.encrypted,
            iops=shared_ebs.iops,
            throughput=shared_ebs.throughput,
            kms_key_id=shared_ebs.kms_key_id,
            size=shared_ebs.size,
            snapshot_id=shared_ebs.snapshot_id,
            volume_type=shared_ebs.volume_type,
            tags=[CfnTag(key="Name", value=shared_ebs.name)],
        )
        volume.cfn_options.deletion_policy = convert_deletion_policy(shared_ebs.deletion_policy)
        return volume.ref

    def _add_wait_condition(self):
        wait_condition_handle = cfn.CfnWaitConditionHandle(self, id="HeadNodeWaitConditionHandle" + self.timestamp)
        wait_condition = cfn.CfnWaitCondition(
            self, id="HeadNodeWaitCondition" + self.timestamp, count=1, handle=wait_condition_handle.ref, timeout="1800"
        )
        return wait_condition, wait_condition_handle

    def _add_head_node(self):
        head_node = self.config.head_node
        head_lt_security_groups = self._get_head_node_security_groups_full()

        # LT network interfaces
        head_lt_nw_interfaces = [
            ec2.CfnLaunchTemplate.NetworkInterfaceProperty(
                device_index=0,
                network_interface_id=self._head_eni.ref,
            )
        ]
        for device_index in range(1, head_node.max_network_interface_count):
            head_lt_nw_interfaces.append(
                ec2.CfnLaunchTemplate.NetworkInterfaceProperty(
                    device_index=device_index,
                    network_card_index=device_index,
                    groups=head_lt_security_groups,
                    subnet_id=head_node.networking.subnet_id,
                )
            )

        # Head node Launch Template
        head_node_launch_template = ec2.CfnLaunchTemplate(
            self,
            "HeadNodeLaunchTemplate",
            launch_template_data=ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
                instance_type=head_node.instance_type,
                cpu_options=ec2.CfnLaunchTemplate.CpuOptionsProperty(core_count=head_node.vcpus, threads_per_core=1)
                if head_node.pass_cpu_options_in_launch_template
                else None,
                block_device_mappings=get_block_device_mappings(head_node.local_storage, self.config.image.os),
                key_name=head_node.ssh.key_name,
                network_interfaces=head_lt_nw_interfaces,
                image_id=self.config.head_node_ami,
                ebs_optimized=head_node.is_ebs_optimized,
                iam_instance_profile=ec2.CfnLaunchTemplate.IamInstanceProfileProperty(
                    name=self._head_node_instance_profile
                ),
                user_data=Fn.base64(
                    Fn.sub(
                        get_user_data_content("../resources/head_node/user_data.sh"),
                        {**get_common_user_data_env(head_node, self.config)},
                    )
                ),
                tag_specifications=[
                    ec2.CfnLaunchTemplate.TagSpecificationProperty(
                        resource_type="instance",
                        tags=get_default_instance_tags(
                            self._stack_name, self.config, head_node, "HeadNode", self.shared_storage_mappings
                        )
                        + get_custom_tags(self.config),
                    ),
                    ec2.CfnLaunchTemplate.TagSpecificationProperty(
                        resource_type="volume",
                        tags=get_default_volume_tags(self._stack_name, "HeadNode") + get_custom_tags(self.config),
                    ),
                ],
            ),
        )

        # Metadata
        head_node_launch_template.add_metadata("Comment", "AWS ParallelCluster Head Node")
        # CloudFormation::Init metadata
        pre_install_action, post_install_action = (None, None)
        if head_node.custom_actions:
            pre_install_action = head_node.custom_actions.on_node_start
            post_install_action = head_node.custom_actions.on_node_configured

        dna_json = json.dumps(
            {
                "cluster": {
                    "stack_name": self._stack_name,
                    "stack_arn": self.stack_id,
                    "byos_substack_arn": self.byos_stack.ref if self.byos_stack else "",
                    "raid_vol_ids": get_shared_storage_ids_by_type(
                        self.shared_storage_mappings, SharedStorageType.RAID
                    ),
                    "raid_parameters": get_shared_storage_options_by_type(
                        self.shared_storage_options, SharedStorageType.RAID
                    ),
                    "disable_hyperthreading_manually": "true"
                    if head_node.disable_simultaneous_multithreading_manually
                    else "false",
                    "base_os": self.config.image.os,
                    "preinstall": pre_install_action.script if pre_install_action else "NONE",
                    "preinstall_args": join_shell_args(pre_install_action.args)
                    if pre_install_action and pre_install_action.args
                    else "NONE",
                    "postinstall": post_install_action.script if post_install_action else "NONE",
                    "postinstall_args": join_shell_args(post_install_action.args)
                    if post_install_action and post_install_action.args
                    else "NONE",
                    "region": self.region,
                    "efs_fs_id": get_shared_storage_ids_by_type(self.shared_storage_mappings, SharedStorageType.EFS),
                    "efs_shared_dir": get_shared_storage_options_by_type(
                        self.shared_storage_options, SharedStorageType.EFS
                    ),  # FIXME
                    "fsx_fs_id": get_shared_storage_ids_by_type(self.shared_storage_mappings, SharedStorageType.FSX),
                    "fsx_mount_name": self.shared_storage_attributes[SharedStorageType.FSX].get("MountName", ""),
                    "fsx_dns_name": self.shared_storage_attributes[SharedStorageType.FSX].get("DNSName", ""),
                    "fsx_options": get_shared_storage_options_by_type(
                        self.shared_storage_options, SharedStorageType.FSX
                    ),
                    "volume": get_shared_storage_ids_by_type(self.shared_storage_mappings, SharedStorageType.EBS),
                    "scheduler": self.config.scheduling.scheduler,
                    "ephemeral_dir": head_node.local_storage.ephemeral_volume.mount_dir
                    if head_node.local_storage and head_node.local_storage.ephemeral_volume
                    else "/scratch",
                    "ebs_shared_dirs": get_shared_storage_options_by_type(
                        self.shared_storage_options, SharedStorageType.EBS
                    ),
                    "proxy": head_node.networking.proxy.http_proxy_address if head_node.networking.proxy else "NONE",
                    "dns_domain": self.scheduler_resources.cluster_hosted_zone.name
                    if self._condition_is_slurm() and self.scheduler_resources.cluster_hosted_zone
                    else "",
                    "hosted_zone": self.scheduler_resources.cluster_hosted_zone.ref
                    if self._condition_is_slurm() and self.scheduler_resources.cluster_hosted_zone
                    else "",
                    "node_type": "HeadNode",
                    "cluster_user": OS_MAPPING[self.config.image.os]["user"],
                    "ddb_table": self.scheduler_resources.dynamodb_table.ref if self._condition_is_slurm() else "NONE",
                    "log_group_name": self.log_group.log_group_name
                    if self.config.monitoring.logs.cloud_watch.enabled
                    else "NONE",
                    "dcv_enabled": "head_node" if self.config.is_dcv_enabled else "false",
                    "dcv_port": head_node.dcv.port if head_node.dcv else "NONE",
                    "enable_intel_hpc_platform": "true" if self.config.is_intel_hpc_platform_enabled else "false",
                    "cw_logging_enabled": "true" if self.config.is_cw_logging_enabled else "false",
                    "cluster_s3_bucket": self.bucket.name,
                    "cluster_config_s3_key": "{0}/configs/{1}".format(
                        self.bucket.artifact_directory, PCLUSTER_S3_ARTIFACTS_DICT.get("config_name")
                    ),
                    "cluster_config_version": self.config.config_version,
                    "instance_types_data_s3_key": f"{self.bucket.artifact_directory}/configs/instance-types-data.json",
                    "custom_node_package": self.config.custom_node_package or "",
                    "custom_awsbatchcli_package": self.config.custom_aws_batch_cli_package or "",
                    "head_node_imds_secured": str(self.config.head_node.imds.secured).lower(),
                },
                "run_list": f"recipe[aws-parallelcluster::{self.config.scheduling.scheduler}_config]",
            },
            indent=4,
        )

        cfn_init = {
            "configSets": {
                "deployFiles": ["deployConfigFiles"],
                "default": [
                    "cfnHupConfig",
                    "chefPrepEnv",
                    "shellRunPreInstall",
                    "chefConfig",
                    "shellRunPostInstall",
                    "chefFinalize",
                ],
                "update": ["deployConfigFiles", "chefUpdate", "sendSignal"],
            },
            "deployConfigFiles": {
                "files": {
                    "/tmp/dna.json": {  # nosec
                        "content": dna_json,
                        "mode": "000644",
                        "owner": "root",
                        "group": "root",
                        "encoding": "plain",
                    },
                    "/etc/chef/client.rb": {
                        "mode": "000644",
                        "owner": "root",
                        "group": "root",
                        "content": "cookbook_path ['/etc/chef/cookbooks']",
                    },
                    "/tmp/extra.json": {  # nosec
                        "mode": "000644",
                        "owner": "root",
                        "group": "root",
                        "content": self.config.extra_chef_attributes,
                    },
                    "/tmp/wait_condition_handle.txt": {  # nosec
                        "mode": "000644",
                        "owner": "root",
                        "group": "root",
                        "content": self.wait_condition_handle.ref,
                    },
                },
                "commands": {
                    "mkdir": {"command": "mkdir -p /etc/chef/ohai/hints"},
                    "touch": {"command": "touch /etc/chef/ohai/hints/ec2.json"},
                    "jq": {
                        "command": (
                            "jq --argfile f1 /tmp/dna.json --argfile f2 /tmp/extra.json -n '$f1 + $f2 "
                            "| .cluster = $f1.cluster + $f2.cluster' > /etc/chef/dna.json "
                            '|| ( echo "jq not installed"; cp /tmp/dna.json /etc/chef/dna.json )'
                        )
                    },
                },
            },
            "cfnHupConfig": {
                "files": {
                    "/etc/cfn/hooks.d/parallelcluster-update.conf": {
                        "content": Fn.sub(
                            (
                                "[parallelcluster-update]\n"
                                "triggers=post.update\n"
                                "path=Resources.HeadNodeLaunchTemplate.Metadata.AWS::CloudFormation::Init\n"
                                "action=PATH=/usr/local/bin:/bin:/usr/bin:/opt/aws/bin; "
                                "cfn-init -v --stack ${StackName} "
                                "--resource HeadNodeLaunchTemplate --configsets update --region ${Region}\n"
                                "runas=root\n"
                            ),
                            {"StackName": self._stack_name, "Region": self.region},
                        ),
                        "mode": "000400",
                        "owner": "root",
                        "group": "root",
                    },
                    "/etc/cfn/cfn-hup.conf": {
                        "content": Fn.sub(
                            "[main]\nstack=${StackId}\nregion=${Region}\ninterval=2",
                            {"StackId": self.stack_id, "Region": self.region},
                        ),
                        "mode": "000400",
                        "owner": "root",
                        "group": "root",
                    },
                }
            },
            "chefPrepEnv": {
                "commands": {
                    "chef": {
                        "command": (
                            "chef-client --local-mode --config /etc/chef/client.rb --log_level info "
                            "--logfile /var/log/chef-client.log --force-formatter --no-color "
                            "--chef-zero-port 8889 --json-attributes /etc/chef/dna.json "
                            "--override-runlist aws-parallelcluster::prep_env"
                        ),
                        "cwd": "/etc/chef",
                    }
                }
            },
            "shellRunPreInstall": {
                "commands": {"runpreinstall": {"command": "/opt/parallelcluster/scripts/fetch_and_run -preinstall"}}
            },
            "chefConfig": {
                "commands": {
                    "chef": {
                        "command": (
                            "chef-client --local-mode --config /etc/chef/client.rb --log_level info "
                            "--logfile /var/log/chef-client.log --force-formatter --no-color "
                            "--chef-zero-port 8889 --json-attributes /etc/chef/dna.json"
                        ),
                        "cwd": "/etc/chef",
                    }
                }
            },
            "shellRunPostInstall": {
                "commands": {"runpostinstall": {"command": "/opt/parallelcluster/scripts/fetch_and_run -postinstall"}}
            },
            "chefFinalize": {
                "commands": {
                    "chef": {
                        "command": (
                            "chef-client --local-mode --config /etc/chef/client.rb --log_level info "
                            "--logfile /var/log/chef-client.log --force-formatter --no-color "
                            "--chef-zero-port 8889 --json-attributes /etc/chef/dna.json "
                            "--override-runlist aws-parallelcluster::finalize"
                        ),
                        "cwd": "/etc/chef",
                    },
                    "bootstrap": {
                        "command": (
                            "[ ! -f /opt/parallelcluster/.bootstrapped ] && echo ${cookbook_version} "
                            "| tee /opt/parallelcluster/.bootstrapped || exit 0"
                        )  # TODO check
                    },
                }
            },
            "chefUpdate": {
                "commands": {
                    "chef": {
                        "command": (
                            "chef-client --local-mode --config /etc/chef/client.rb --log_level info "
                            "--logfile /var/log/chef-client.log --force-formatter --no-color "
                            "--chef-zero-port 8889 --json-attributes /etc/chef/dna.json "
                            "--override-runlist aws-parallelcluster::update_head_node || "
                            "cfn-signal --exit-code=1 --reason='Chef client failed' "
                            f"'{self.wait_condition_handle.ref}'"
                        ),
                        "cwd": "/etc/chef",
                    }
                }
            },
            "sendSignal": {
                "commands": {
                    "sendSignal": {
                        "command": f"cfn-signal --exit-code=0 --reason='HeadNode setup complete' "
                        f"'{self.wait_condition_handle.ref}'"
                    }
                }
            },
        }

        if not self._condition_is_batch():
            cfn_init["deployConfigFiles"]["files"]["/opt/parallelcluster/shared/launch_templates_config.json"] = {
                "mode": "000644",
                "owner": "root",
                "group": "root",
                "content": self._get_launch_templates_config(),
            }

        head_node_launch_template.add_metadata("AWS::CloudFormation::Init", cfn_init)
        head_node_instance = ec2.CfnInstance(
            self,
            "HeadNode",
            launch_template=ec2.CfnInstance.LaunchTemplateSpecificationProperty(
                launch_template_id=head_node_launch_template.ref,
                version=head_node_launch_template.attr_latest_version_number,
            ),
        )
        if not self._condition_is_batch():
            head_node_instance.node.add_dependency(self.compute_fleet_resources)

        if self._condition_is_byos() and self.byos_stack:
            head_node_instance.add_depends_on(self.byos_stack)

        return head_node_instance

    def _get_launch_templates_config(self):
        if not self.compute_fleet_resources:
            return None

        lt_config = {"Queues": {}}
        for queue, compute_resouces in self.compute_fleet_resources.compute_launch_templates.items():
            lt_config["Queues"][queue] = {"ComputeResources": {}}
            for compute_resource, launch_template in compute_resouces.items():
                lt_config["Queues"][queue]["ComputeResources"][compute_resource] = {
                    "LaunchTemplate": {"Id": launch_template.ref, "Version": launch_template.attr_latest_version_number}
                }

        return lt_config

    def _add_byos_substack(self):
        self.byos_stack = None
        if not self._condition_is_byos() or not get_attr(
            self.config, "scheduling.settings.scheduler_definition.cluster_infrastructure.cloud_formation.template"
        ):
            return

        template_url = self.bucket.get_cfn_template_url(
            template_name=PCLUSTER_S3_ARTIFACTS_DICT.get("byos_template_name")
        )

        self.byos_stack = CfnStack(self, "ByosStack", template_url=template_url, parameters={})

    # -- Conditions -------------------------------------------------------------------------------------------------- #

    def _condition_create_lambda_iam_role(self):
        return (
            not self.config.iam
            or not self.config.iam.roles
            or not self.config.iam.roles.lambda_functions_role
            or self.config.iam.roles.get_param("lambda_functions_role").implied
        )

    def _condition_is_slurm(self):
        return self.config.scheduling.scheduler == "slurm"

    def _condition_is_byos(self):
        return self.config.scheduling.scheduler == "byos"

    def _condition_is_batch(self):
        return self.config.scheduling.scheduler == "awsbatch"

    # -- Outputs ----------------------------------------------------------------------------------------------------- #

    def _add_outputs(self):
        # Storage filesystem Ids
        for storage_type, storage_list in self.shared_storage_mappings.items():
            CfnOutput(
                self,
                "{0}Ids".format(storage_type.name),
                description="{0} Filesystem IDs".format(storage_type.name),
                value=",".join(storage.id for storage in storage_list),
            )

        CfnOutput(
            self,
            "HeadNodeInstanceID",
            description="ID of the head node instance",
            value=self.head_node_instance.ref,
        )

        CfnOutput(
            self,
            "HeadNodePrivateIP",
            description="Private IP Address of the head node",
            value=self.head_node_instance.attr_private_ip,
        )

        CfnOutput(
            self,
            "HeadNodePrivateDnsName",
            description="Private DNS name of the head node",
            value=self.head_node_instance.attr_private_dns_name,
        )


class ComputeFleetConstruct(Construct):
    """Construct defining compute fleet specific resources."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        cluster_config: SlurmClusterConfig,
        log_group: logs.CfnLogGroup,
        cleanup_lambda: awslambda.CfnFunction,
        cleanup_lambda_role: iam.CfnRole,
        compute_security_group: ec2.CfnSecurityGroup,
        shared_storage_mappings: Dict,
        shared_storage_options: Dict,
        shared_storage_attributes: Dict,
        compute_node_instance_profiles: Dict[str, str],
        cluster_hosted_zone,
        dynamodb_table,
    ):
        super().__init__(scope, id)
        self._cleanup_lambda = cleanup_lambda
        self._cleanup_lambda_role = cleanup_lambda_role
        self._compute_security_group = compute_security_group
        self._config = cluster_config
        self._shared_storage_mappings = shared_storage_mappings
        self._shared_storage_attributes = shared_storage_attributes
        self._shared_storage_options = shared_storage_options
        self._log_group = log_group
        self._cluster_hosted_zone = cluster_hosted_zone
        self._dynamodb_table = dynamodb_table
        self._compute_node_instance_profiles = compute_node_instance_profiles

        self._add_resources()

    # -- Utility methods --------------------------------------------------------------------------------------------- #

    @property
    def stack_name(self):
        """Name of the CFN stack."""
        return Stack.of(self).stack_name

    # -- Resources --------------------------------------------------------------------------------------------------- #

    def _add_resources(self):
        managed_placement_groups = self._add_placement_groups()
        self.compute_launch_templates = self._add_launch_templates(
            managed_placement_groups, self._compute_node_instance_profiles
        )
        custom_resource_deps = list(managed_placement_groups.values())
        if self._compute_security_group:
            custom_resource_deps.append(self._compute_security_group)
        self._add_cleanup_custom_resource(dependencies=custom_resource_deps)

    def _add_cleanup_custom_resource(self, dependencies: List[CfnResource]):
        terminate_compute_fleet_custom_resource = CfnCustomResource(
            self,
            "TerminateComputeFleetCustomResource",
            service_token=self._cleanup_lambda.attr_arn,
        )
        terminate_compute_fleet_custom_resource.add_property_override("StackName", self.stack_name)
        terminate_compute_fleet_custom_resource.add_property_override("Action", "TERMINATE_EC2_INSTANCES")
        for dep in dependencies:
            terminate_compute_fleet_custom_resource.add_depends_on(dep)

        if self._cleanup_lambda_role:
            self._add_policies_to_cleanup_resources_lambda_role()

    def _add_policies_to_cleanup_resources_lambda_role(self):
        self._cleanup_lambda_role.policies[0].policy_document.add_statements(
            iam.PolicyStatement(
                actions=["ec2:DescribeInstances"],
                resources=["*"],
                effect=iam.Effect.ALLOW,
                sid="DescribeInstances",
            ),
            iam.PolicyStatement(
                actions=["ec2:TerminateInstances"],
                resources=["*"],
                effect=iam.Effect.ALLOW,
                conditions={"StringEquals": {f"ec2:ResourceTag/{PCLUSTER_CLUSTER_NAME_TAG}": self.stack_name}},
                sid="FleetTerminatePolicy",
            ),
        )

    def _add_placement_groups(self) -> Dict[str, ec2.CfnPlacementGroup]:
        managed_placement_groups = {}
        for queue in self._config.scheduling.queues:
            if (
                queue.networking.placement_group
                and queue.networking.placement_group.enabled
                and not queue.networking.placement_group.id
            ):
                managed_placement_groups[queue.name] = ec2.CfnPlacementGroup(
                    self, f"PlacementGroup{create_hash_suffix(queue.name)}", strategy="cluster"
                )
        return managed_placement_groups

    def _add_launch_templates(self, managed_placement_groups, instance_profiles):
        compute_launch_templates = {}
        for queue in self._config.scheduling.queues:
            compute_launch_templates[queue.name] = {}
            queue_lt_security_groups = get_queue_security_groups_full(self._compute_security_group, queue)

            queue_placement_group = None
            if queue.networking.placement_group and queue.networking.placement_group.enabled:
                if queue.networking.placement_group.id:
                    queue_placement_group = queue.networking.placement_group.id
                else:
                    queue_placement_group = managed_placement_groups[queue.name].ref

            queue_pre_install_action, queue_post_install_action = (None, None)
            if queue.custom_actions:
                queue_pre_install_action = queue.custom_actions.on_node_start
                queue_post_install_action = queue.custom_actions.on_node_configured

            for compute_resource in queue.compute_resources:
                launch_template = self._add_compute_resource_launch_template(
                    queue,
                    compute_resource,
                    queue_pre_install_action,
                    queue_post_install_action,
                    queue_lt_security_groups,
                    queue_placement_group,
                    instance_profiles,
                )
                compute_launch_templates[queue.name][compute_resource.name] = launch_template
        return compute_launch_templates

    def _add_compute_resource_launch_template(
        self,
        queue,
        compute_resource,
        queue_pre_install_action,
        queue_post_install_action,
        queue_lt_security_groups,
        queue_placement_group,
        instance_profiles,
    ):
        # LT network interfaces
        compute_lt_nw_interfaces = [
            ec2.CfnLaunchTemplate.NetworkInterfaceProperty(
                device_index=0,
                associate_public_ip_address=queue.networking.assign_public_ip
                if compute_resource.max_network_interface_count == 1
                else None,  # parameter not supported for instance types with multiple network interfaces
                interface_type="efa" if compute_resource.efa and compute_resource.efa.enabled else None,
                groups=queue_lt_security_groups,
                subnet_id=queue.networking.subnet_ids[0],
            )
        ]
        for device_index in range(1, compute_resource.max_network_interface_count):
            compute_lt_nw_interfaces.append(
                ec2.CfnLaunchTemplate.NetworkInterfaceProperty(
                    device_index=device_index,
                    network_card_index=device_index,
                    interface_type="efa" if compute_resource.efa and compute_resource.efa.enabled else None,
                    groups=queue_lt_security_groups,
                    subnet_id=queue.networking.subnet_ids[0],
                )
            )

        instance_market_options = None
        if queue.capacity_type == CapacityType.SPOT:
            instance_market_options = ec2.CfnLaunchTemplate.InstanceMarketOptionsProperty(
                market_type="spot",
                spot_options=ec2.CfnLaunchTemplate.SpotOptionsProperty(
                    spot_instance_type="one-time",
                    instance_interruption_behavior="terminate",
                    max_price=None if compute_resource.spot_price is None else str(compute_resource.spot_price),
                ),
            )

        return ec2.CfnLaunchTemplate(
            self,
            # FIXME change to compute_resourece.name
            f"LaunchTemplate{create_hash_suffix(queue.name + compute_resource.instance_type)}",
            launch_template_name=f"{self.stack_name}-{queue.name}-{compute_resource.instance_type}",
            launch_template_data=ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
                instance_type=compute_resource.instance_type,
                cpu_options=ec2.CfnLaunchTemplate.CpuOptionsProperty(
                    core_count=compute_resource.vcpus, threads_per_core=1
                )
                if compute_resource.pass_cpu_options_in_launch_template
                else None,
                block_device_mappings=get_block_device_mappings(
                    queue.compute_settings.local_storage, self._config.image.os
                ),
                # key_name=,
                network_interfaces=compute_lt_nw_interfaces,
                placement=ec2.CfnLaunchTemplate.PlacementProperty(group_name=queue_placement_group),
                image_id=self._config.image_dict[queue.name],
                ebs_optimized=compute_resource.is_ebs_optimized,
                iam_instance_profile=ec2.CfnLaunchTemplate.IamInstanceProfileProperty(
                    name=instance_profiles[queue.name]
                ),
                instance_market_options=instance_market_options,
                user_data=Fn.base64(
                    Fn.sub(
                        get_user_data_content("../resources/compute_node/user_data.sh"),
                        {
                            **{
                                "EnableEfa": "efa" if compute_resource.efa and compute_resource.efa.enabled else "NONE",
                                "RAIDOptions": get_shared_storage_options_by_type(
                                    self._shared_storage_options, SharedStorageType.RAID
                                ),
                                "DisableHyperThreadingManually": "true"
                                if compute_resource.disable_simultaneous_multithreading_manually
                                else "false",
                                "BaseOS": self._config.image.os,
                                "PreInstallScript": queue_pre_install_action.script
                                if queue_pre_install_action
                                else "NONE",
                                "PreInstallArgs": join_shell_args(queue_pre_install_action.args)
                                if queue_pre_install_action and queue_pre_install_action.args
                                else "NONE",
                                "PostInstallScript": queue_post_install_action.script
                                if queue_post_install_action
                                else "NONE",
                                "PostInstallArgs": join_shell_args(queue_post_install_action.args)
                                if queue_post_install_action and queue_post_install_action.args
                                else "NONE",
                                "EFSId": get_shared_storage_ids_by_type(
                                    self._shared_storage_mappings, SharedStorageType.EFS
                                ),
                                "EFSOptions": get_shared_storage_options_by_type(
                                    self._shared_storage_options, SharedStorageType.EFS
                                ),
                                "FSXId": get_shared_storage_ids_by_type(
                                    self._shared_storage_mappings, SharedStorageType.FSX
                                ),
                                "FSXMountName": self._shared_storage_attributes[SharedStorageType.FSX].get(
                                    "MountName", ""
                                ),
                                "FSXDNSName": self._shared_storage_attributes[SharedStorageType.FSX].get("DNSName", ""),
                                "FSXOptions": get_shared_storage_options_by_type(
                                    self._shared_storage_options, SharedStorageType.FSX
                                ),
                                "Scheduler": self._config.scheduling.scheduler,
                                "EphemeralDir": queue.compute_settings.local_storage.ephemeral_volume.mount_dir
                                if queue.compute_settings
                                and queue.compute_settings.local_storage
                                and queue.compute_settings.local_storage.ephemeral_volume
                                else "/scratch",
                                "EbsSharedDirs": get_shared_storage_options_by_type(
                                    self._shared_storage_options, SharedStorageType.EBS
                                ),
                                "ClusterDNSDomain": str(self._cluster_hosted_zone.name)
                                if self._cluster_hosted_zone
                                else "",
                                "ClusterHostedZone": str(self._cluster_hosted_zone.ref)
                                if self._cluster_hosted_zone
                                else "",
                                "OSUser": OS_MAPPING[self._config.image.os]["user"],
                                "DynamoDBTable": self._dynamodb_table.ref if self._dynamodb_table else "NONE",
                                "LogGroupName": self._log_group.log_group_name
                                if self._config.monitoring.logs.cloud_watch.enabled
                                else "NONE",
                                "IntelHPCPlatform": "true" if self._config.is_intel_hpc_platform_enabled else "false",
                                "CWLoggingEnabled": "true" if self._config.is_cw_logging_enabled else "false",
                                "QueueName": queue.name,
                                "ComputeResourceName": compute_resource.name,
                                "EnableEfaGdr": "compute"
                                if compute_resource.efa and compute_resource.efa.gdr_support
                                else "NONE",
                                "CustomNodePackage": self._config.custom_node_package or "",
                                "CustomAwsBatchCliPackage": self._config.custom_aws_batch_cli_package or "",
                                "ExtraJson": self._config.extra_chef_attributes,
                            },
                            **get_common_user_data_env(queue, self._config),
                        },
                    )
                ),
                monitoring=ec2.CfnLaunchTemplate.MonitoringProperty(enabled=False),
                tag_specifications=[
                    ec2.CfnLaunchTemplate.TagSpecificationProperty(
                        resource_type="instance",
                        tags=get_default_instance_tags(
                            self.stack_name, self._config, compute_resource, "Compute", self._shared_storage_mappings
                        )
                        + [CfnTag(key=PCLUSTER_QUEUE_NAME_TAG, value=queue.name)]
                        + get_custom_tags(self._config),
                    ),
                    ec2.CfnLaunchTemplate.TagSpecificationProperty(
                        resource_type="volume",
                        tags=get_default_volume_tags(self.stack_name, "Compute")
                        + [CfnTag(key=PCLUSTER_QUEUE_NAME_TAG, value=queue.name)]
                        + get_custom_tags(self._config),
                    ),
                ],
            ),
        )
