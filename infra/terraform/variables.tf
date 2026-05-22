variable "project_id" {
  description = "GCP project ID that hosts all Sketch resources."
  type        = string
}

variable "region" {
  description = "GCP region for regional resources (Artifact Registry, etc.)."
  type        = string
  default     = "us-west1"
}

variable "zone" {
  description = "GCP zone for the GCE VM."
  type        = string
  default     = "us-west1-a"
}

variable "github_owner" {
  description = "GitHub owner (user or org) that holds the repo. Used to scope Workload Identity Federation."
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name."
  type        = string
  default     = "sketch"
}

variable "guild_config" {
  description = "Per-guild routing. Keys are Discord guild IDs (as strings). Each value names the spreadsheet that guild writes to. The VM service account must be shared on every listed sheet as an Editor (manual one-time step)."
  type = map(object({
    spreadsheet_id = string
  }))
}

variable "dev_guild_id" {
  description = "DEV ONLY: Discord guild ID for fast slash-command sync. Leave empty to register globally (~1 hour propagation). Independent of guild_config — this only controls where slash commands are registered, not which guilds the bot serves."
  type        = string
  default     = ""
}

variable "image_tag" {
  description = "Container image tag the systemd unit pulls. Override at apply time (or via tfvars) to roll back to a specific :sha-xxxxx tag."
  type        = string
  default     = "current"
}
