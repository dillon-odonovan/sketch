terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  # State bucket is supplied at init time so it can be different per environment.
  # See README.md "Bootstrap" — the bucket itself is not Terraform-managed
  # (chicken-and-egg).
  backend "gcs" {
    prefix = "sketch/prod"
  }
}
