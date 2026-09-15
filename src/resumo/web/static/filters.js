// Abre/fecha o painel de filtros e mantém o contador do botão em dia.
//
// Só isso: quem aplica o filtro continua sendo o htmx (site dinâmico) ou o
// predicado de DOM embutido no index_static.html. Este arquivo é compartilhado
// pelas duas versões e não pode saber qual delas está na tela.
(function () {
  var dialogo = document.getElementById("filtros");
  if (!dialogo || typeof dialogo.showModal !== "function") return;

  var abrir = document.querySelector("[data-filtros-abrir]");
  var contagem = document.querySelector("[data-filtros-contagem]");
  var controles = Array.prototype.slice.call(
    dialogo.querySelectorAll("select, input"));

  // Quantos filtros estreitam a busca: um <select> escolhido, um interruptor ligado.
  function ativos() {
    return controles.filter(function (el) {
      return el.tagName === "SELECT" ? !!el.value : el.checked;
    }).length;
  }

  function atualizar() {
    var n = ativos();
    if (!contagem) return;
    contagem.textContent = n;
    contagem.hidden = n === 0;
    if (abrir) {
      abrir.setAttribute(
        "aria-label", n === 0 ? "Filtros" : "Filtros (" + n + " ativos)");
    }
  }

  function limpar() {
    // Um único evento no fim: cada controle carrega o estado do painel inteiro
    // (hx-include no dinâmico, leitura completa no estático), então disparar em
    // todos os limpos só geraria requisições repetidas para o mesmo resultado.
    var ultimo = null;
    controles.forEach(function (el) {
      if (el.type === "checkbox") {
        if (!el.checked) return;
        el.checked = false;
      } else if (el.tagName === "SELECT") {
        if (!el.value) return;
        el.value = "";
      } else {
        return;
      }
      ultimo = el;
    });
    atualizar();
    if (ultimo) ultimo.dispatchEvent(new Event("change", { bubbles: true }));
  }

  if (abrir) {
    abrir.hidden = false;
    abrir.addEventListener("click", function () { dialogo.showModal(); });
  }
  Array.prototype.slice.call(dialogo.querySelectorAll("[data-filtros-fechar]"))
    .forEach(function (botao) {
      botao.addEventListener("click", function () { dialogo.close(); });
    });
  var botaoLimpar = dialogo.querySelector("[data-filtros-limpar]");
  if (botaoLimpar) botaoLimpar.addEventListener("click", limpar);
  controles.forEach(function (el) { el.addEventListener("change", atualizar); });

  // Clique no backdrop: o alvo do evento é o próprio <dialog> só quando o
  // ponteiro cai fora do conteúdo.
  dialogo.addEventListener("click", function (evento) {
    if (evento.target === dialogo) dialogo.close();
  });

  atualizar();
})();
