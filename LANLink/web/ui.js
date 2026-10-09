'use strict';

window.LANUI = (() => {
  function avatar(user, extraClass = '') {
    const name = String(user.name || 'Пользователь').trim();
    const parts = name.split(/[\s@._-]+/).filter(Boolean);
    const initials = (parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2)).toLocaleUpperCase('ru-RU');
    const node = document.createElement('span');
    node.className = 'avatar tone-' + (Math.abs(Number(user.id) || 0) % 6) + (extraClass ? ' ' + extraClass : '');
    node.textContent = initials;
    node.title = name;
    if (user.avatar_file) {
      const image = document.createElement('img');
      image.src = '/avatar/' + encodeURIComponent(user.id) + '?v=' + encodeURIComponent(user.avatar_file);
      image.alt = '';
      image.loading = 'lazy';
      image.onerror = () => image.remove();
      node.append(image);
    }
    return node;
  }

  function ask(options) {
    return new Promise(resolve => {
      const dialog = document.createElement('dialog');
      dialog.className = 'action-dialog';
      const form = document.createElement('form');
      form.noValidate = true;
      const title = document.createElement('h3');
      title.textContent = options.title;
      form.append(title);
      if (options.description) {
        const description = document.createElement('p');
        description.className = 'dialog-description';
        description.textContent = options.description;
        form.append(description);
      }
      let field = null;
      if (options.kind !== 'confirm') {
        const label = document.createElement('label');
        label.textContent = options.label || '';
        if (options.kind === 'select') {
          field = document.createElement('select');
          for (const choice of options.choices || []) {
            const option = document.createElement('option');
            option.value = String(choice.value);
            option.textContent = choice.label;
            field.append(option);
          }
        } else if (options.kind === 'textarea') {
          field = document.createElement('textarea');
          field.rows = 5;
        } else {
          field = document.createElement('input');
          field.type = options.kind === 'number' ? 'number' : 'text';
          if (options.kind === 'number') {
            field.min = String(options.min || 0);
            field.max = String(options.max || 60);
            field.step = '1';
          }
        }
        if (options.maxLength) field.maxLength = options.maxLength;
        if (options.value !== undefined) field.value = String(options.value);
        label.append(field);
        form.append(label);
      }
      const error = document.createElement('p');
      error.className = 'dialog-error';
      error.setAttribute('role', 'alert');
      form.append(error);
      const actions = document.createElement('div');
      actions.className = 'dialog-actions';
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.textContent = 'Отмена';
      cancel.onclick = () => dialog.close();
      const submit = document.createElement('button');
      submit.type = 'submit';
      submit.className = options.danger ? 'dialog-danger' : 'primary';
      submit.textContent = options.submit || 'Сохранить';
      actions.append(cancel, submit);
      form.append(actions);
      dialog.append(form);
      let answer = null;
      form.onsubmit = event => {
        event.preventDefault();
        const value = field ? field.value.trim() : true;
        const problem = options.validate?.(value) || '';
        if (problem) {
          error.textContent = problem;
          field?.focus();
          return;
        }
        answer = value;
        dialog.close();
      };
      dialog.addEventListener('close', () => {
        dialog.remove();
        resolve(answer);
      }, {once: true});
      document.body.append(dialog);
      dialog.showModal();
      field?.focus();
    });
  }

  return {avatar, ask};
})();
