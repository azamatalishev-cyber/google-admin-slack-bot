data "aws_ssm_parameter" "service_account_json" {
  name = "/it/it-slack-bot/service-account.json"
}

data "aws_ssm_parameter" "env_variables_file" {
  name = "/it/it-slack-bot/env-variable-definitions"
}

locals {
  namespace   = "it"
  name        = "it-slack-bot"
  provisioner = "terraform"
  default_labels = {
    provisoner = "terraform"
    app        = "it-slack-bot"
  }
  secrets_checksum = sha256(
    join("", data.aws_ssm_parameter.service_account_json.value, data.aws_ssm_parameter.env_variables_file.value)
  )
}

provider "kubernetes" {
  config_path = "~/.kube/config"
}

resource "kubernetes_secret" "it_slack_bot_credentials" {
  metadata {
    name      = "${local.name}-credentials"
    namespace = local.namespace
  }
  data = {
    "service_account.json" = data.aws_ssm_parameter.service_account_json.value
    ".env"                 = data.aws_ssm_parameter.env_variables_file.value
  }
}

resource "kubernetes_network_policy" "it_slack_bot" {
  metadata {
    name      = local.name
    namespace = local.namespace
    labels    = local.default_labels

  }
  spec {
    pod_selector {
      match_labels = {
        app = local.default_labels.app
      }
    }
    egress {
      to {
        ip_block {
          cidr = "0.0.0.0/0"
          except = [
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16"
          ]
        }
      }
    }
    egress {
      to {
        ip_block {
          cidr = "10.88.0.0/16"

        }
        namespace_selector {
          match_labels {
            name = "kube-system"
          }
        }
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
      ports {
        port     = "53"
        protocol = "UDP"
      }
    }
    policy_types = ["Egress"]
  }
}


resource "kubernetes_ingress" "it_slack_bot" {
  metadata {
    name = local.name
    annotations = {
      "kubernetes.io/ingress.class" = "pub-nginx-generic"
    }
  }

  spec {
    rule {
      host = "slackbothere.com"
      http {
        path {
          backend {
            service_name = local.name
            service_port = 8000
          }
          path = "/"
        }
      }
    }
  }
}

resource "kubernetes_service" "it_slack_bot" {
  metadata {
    name      = local.name
    namespace = local.namespace
  }
  spec {
    selector = {
      app = local.default_labels.app
    }
    port {
      port        = 8000
      target_port = "http"
    }
    type = "ClusterIP"
  }
}

resource "kubernetes_deployment" "it_slack_bot" {
  metadata {
    name      = local.name
    namespace = local.namespace
    labels    = local.default_labels
  }

  spec {
    replicas = 2 

    selector {
      match_labels = {
        app = local.default_labels.app
      }
    }

    template {
      metadata {
        labels = {
          app              = local.default_labels.app
          secrets_checksum = local.secrets_checksum
        }
      }

      spec {
        volume {
          name = "bot-credentials"
          secret {
            secret_name = "${local.name}-credentials"
          }
        }
        container {
          image             = "it_slack_bot:v1.0.0"
          name              = "main"
          image_pull_policy = "IfNotPresent"
          port {
            container_port = 8000
            name           = "http"
          }
          volume_mount {
            name       = "bot-credentials"
            mount_path = "/credentials"
          }
          resources {
            limits = {
              cpu    = "500m"
              memory = "512Mi"
            }
            requests = {
              cpu    = "250m"
              memory = "50Mi"
            }
          }
        }
      }
    }
  }
}
