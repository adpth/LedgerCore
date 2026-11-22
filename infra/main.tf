terraform {
  required_version = ">= 1.8"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" { type = string }
variable "name" {
  type    = string
  default = "ledgercore"
}
variable "cluster_version" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "github_deploy_role_arn" { type = string }

provider "aws" { region = var.region }

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.30.2"

  name                                     = var.name
  kubernetes_version                       = var.cluster_version
  endpoint_public_access                   = false
  enable_cluster_creator_admin_permissions = true
  vpc_id                                   = var.vpc_id
  subnet_ids                               = var.private_subnet_ids

  eks_managed_node_groups = {
    ledger = {
      instance_types = ["m7i.large"]
      min_size       = 2
      max_size       = 4
      desired_size   = 2
    }
  }

  access_entries = {
    deploy = {
      principal_arn = var.github_deploy_role_arn
      policy_associations = {
        deploy = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"
          access_scope = {
            type       = "namespace"
            namespaces = ["ledgercore"]
          }
        }
      }
    }
  }
}

resource "aws_db_subnet_group" "ledger" {
  name       = var.name
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "database" {
  name   = "${var.name}-database"
  vpc_id = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "postgres" {
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = module.eks.node_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_db_instance" "ledger" {
  identifier                  = var.name
  engine                      = "postgres"
  engine_version              = "17"
  instance_class              = "db.m7g.large"
  db_name                     = "ledger"
  username                    = "ledger_admin"
  manage_master_user_password = true
  allocated_storage           = 30
  max_allocated_storage       = 100
  storage_encrypted           = true
  multi_az                    = true
  publicly_accessible         = false
  db_subnet_group_name        = aws_db_subnet_group.ledger.name
  vpc_security_group_ids      = [aws_security_group.database.id]
  backup_retention_period     = 7
  deletion_protection         = true
  final_snapshot_identifier   = "${var.name}-final"
}

resource "aws_ecr_repository" "ledger" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

output "database_endpoint" { value = aws_db_instance.ledger.endpoint }
output "ecr_repository" { value = aws_ecr_repository.ledger.repository_url }
output "cluster_name" { value = module.eks.cluster_name }
